"""S4 probe plugin: the voice platform's exec approvals become Telegram cards.

The model of the stage-5 adapter. Imports only modules listed in Hermes' ``public-api.md``
(``gateway.config``, ``gateway.platforms.base``, ``gateway.platforms.event``,
``gateway.platforms._shared``, ``hermes_constants``, ``tools.approval.resolve_gateway_approval`` /
``resolve_gateway_approval_card``); python-telegram-bot is imported inside functions only.

Path: ``_send_exec_approval_prompt`` (overridden, so the gateway takes the button path) renders a
card from the public ``ExecApprovalPrompt`` and sends it with the bot of the DEFAULT profile's
Telegram adapter (the PTB ``Application`` a ``register_platform_handler("telegram", ...)`` factory
receives). A ``CallbackQueryHandler(pattern="^hya:")`` takes presses: only the owner, only in the
configured chat, only a choice the card offered -> ``resolve_gateway_approval_card``. Any delivery
failure, a 10 s send deadline or disabled approvals -> immediate deny by request id.

Logs never carry the command, the reason or a token: lengths, request ids and outcomes only.
``JOURNAL`` is for the probe harness.
"""

import asyncio
import html
import logging
import time

from gateway.config import Platform
from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from hermes_constants import get_hermes_home
from tools.approval import resolve_gateway_approval, resolve_gateway_approval_card

from . import approval_book as book_mod

PLATFORM = "yandex_alice"
SEND_DEADLINE_S = 10.0  # below the gateway's 15 s "ambiguous" cut-off
DENY_REASON = ("Автоматический отказ плагина: карточка подтверждения не доставлена в Telegram, "
               "владелец её не видел")
TOAST = {
    book_mod.WAIT: "Подождите секунду и нажмите снова",
    book_mod.STALE: "Запрос неактуален",
    book_mod.WRONG_CHAT: "Отвечать может только владелец",
    book_mod.NOT_OWNER: "Отвечать может только владелец",
    book_mod.NOT_OFFERED: "Этот выбор недоступен",
}
RESULT_LINE = {"once": "✅ Разрешено один раз", "deny": "❌ Отклонено"}
STALE_LINE = "⌛ Запрос неактуален"

logger = logging.getLogger(__name__)
JOURNAL: list = []
STATE: dict = {"plugin_home": None, "apps": [], "adapter": None}


def _rec(op, **kw):
    JOURNAL.append(dict(t=time.monotonic(), op=op, **kw))


def _token_of(app):
    try:
        return getattr(app.bot, "token", None)
    except Exception:  # noqa: BLE001 - an app without a bot yet
        return None


class S4Adapter(BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self, config):
        super().__init__(config, Platform(PLATFORM))
        self.book = book_mod.ApprovalBook()
        self._own_token = None
        self._n = 0
        STATE["adapter"] = self

    # -- lifecycle --

    async def connect(self, *, is_reconnect=False):
        chat = get_scoped_secret("ALICE_TELEGRAM_CHAT", "")
        owner = get_scoped_secret("ALICE_TELEGRAM_OWNER_ID", "")
        self._own_token = (get_scoped_secret("TELEGRAM_BOT_TOKEN", "") or "").strip() or None
        self.book = book_mod.ApprovalBook(chat, owner)
        _rec("connect", home=str(get_hermes_home()), enabled=self.book.enabled,
             chat_set=self.book.chat is not None, owner_set=self.book.owner is not None,
             token_set=self._own_token is not None)
        self._running = True
        self._mark_connected()
        return True

    async def disconnect(self):
        self._running = False
        self._mark_disconnected()

    async def get_chat_info(self, chat_id):
        return {"name": "chat-%s" % chat_id, "type": "dm", "chat_id": chat_id}

    async def send_typing(self, chat_id, metadata=None):
        return None

    # -- the voice side (journal only) --

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self._n += 1
        meta = dict(metadata or {})
        refuse = book_mod.is_full_command_block(content or "", meta)
        _rec("send", chat=str(chat_id), content=(content or "")[:200], length=len(content or ""),
             reply_to=None if reply_to is None else str(reply_to), notify=meta.get("notify"),
             interim=bool(meta.get("_interim_send")), approval_prompt=bool(meta.get("is_approval_prompt")),
             refused=refuse)
        if refuse:  # keep Hermes' Deny-only mark: nobody saw this command in Telegram
            return SendResult(success=False, error="full command is not shown to the owner")
        return SendResult(success=True, message_id="out-%d" % self._n)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        mid = str(message_id)
        _rec("edit", chat=str(chat_id), message_id=mid, length=len(content or ""),
             card=mid.startswith("hya-card:"), head=(content or "")[:60])
        if not mid.startswith("hya-card:"):
            return SendResult(success=False, error="Not supported")
        card = self.book.expire(mid, "hermes_timeout_notice")
        if card is None or card.tg_message_id is None:
            return SendResult(success=False, error="card is not open")
        ok = await self._edit_card(card, html.escape((content or "")[:3500]))
        return SendResult(success=ok, message_id=mid)

    async def delete_message(self, chat_id, message_id):
        return False

    async def on_processing_start(self, event):
        _rec("start", chat=str(event.source.chat_id), mid=event.message_id,
             profile=getattr(event.source, "profile", None))

    async def on_processing_complete(self, event, outcome):
        closed = self.book.close_chat(str(event.source.chat_id))
        _rec("complete", chat=str(event.source.chat_id), mid=event.message_id,
             outcome=getattr(outcome, "value", str(outcome)), closed=[c.card_id for c in closed])

    async def probe_inject(self, text, message_id, chat_id, user_id):
        source = self.build_source(chat_id=chat_id, chat_name="chat-%s" % chat_id, chat_type="dm",
                                   user_id=user_id, user_name="probe")
        await self.handle_message(MessageEvent(text=text, message_type=MessageType.TEXT,
                                               source=source, message_id=message_id))

    # -- approvals --

    def _bot(self):
        """The default profile's bot: a factory call from the plugin's own home whose bot token is
        the one this adapter read in connect(). None when there is no such app or it is ambiguous."""
        apps = [a for home, a in STATE["apps"] if home == STATE["plugin_home"]
                and self._own_token is not None and _token_of(a) == self._own_token]
        unique = {id(a): a for a in apps}
        if len(unique) != 1:
            _rec("bot_pick", candidates=len(STATE["apps"]), matched=len(unique))
            return None
        return next(iter(unique.values())).bot

    def _deny(self, card, why):
        count = resolve_gateway_approval(card.session_key, "deny", reason=DENY_REASON,
                                         request_id=card.request_id)
        _rec("deny", card=card.card_id, request_id=card.request_id, why=why, count=count)
        logger.info("S4 approval denied without the owner (request_id=%s, why=%s, count=%s)",
                    card.request_id, why, count)
        return count

    async def _send_exec_approval_prompt(self, prompt):
        text, whole = book_mod.render_card(prompt.command, prompt.description, prompt.smart_denied)
        choices = book_mod.offered_choices(prompt.actions, whole)
        _rec("prompt", chat=str(prompt.chat_id), home=str(get_hermes_home()), request_id=prompt.request_id,
             session_key=prompt.session_key, command_len=len(prompt.command or ""),
             hermes_choices=[c for _l, c, _s in prompt.actions], choices=list(choices), whole=whole,
             smart_denied=prompt.smart_denied)
        if not prompt.request_id:
            return SendResult(success=False, error="no request id")
        card = self.book.open(prompt.session_key, prompt.request_id, str(prompt.chat_id), choices, text)
        if card is None:
            return SendResult(success=False, error="no session key")
        bot =self._bot() if self.book.enabled else None
        if bot is None:
            self.book.fail(card.card_id, "disabled" if not self.book.enabled else "no_bot")
            self._deny(card, "disabled" if not self.book.enabled else "no_bot")
            return SendResult(success=False, error="approvals unavailable")
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        labels = {"once": "Разрешить один раз", "deny": "Отклонить"}
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(labels[c], callback_data=book_mod.callback_data(
            c, card.token)) for c in card.choices]])
        task = asyncio.ensure_future(bot.send_message(chat_id=self.book.chat, text=text, parse_mode="HTML",
                                                      reply_markup=markup))
        try:
            msg = await asyncio.wait_for(asyncio.shield(task), SEND_DEADLINE_S)
        except asyncio.TimeoutError:
            if self.book.fail(card.card_id, "deadline"):
                self._deny(card, "deadline")
            task.add_done_callback(lambda t: asyncio.ensure_future(self._late(card, t)))
            return SendResult(success=False, error="approval card send timed out")
        except Exception as exc:  # noqa: BLE001 - any send failure denies
            if self.book.fail(card.card_id, type(exc).__name__):
                self._deny(card, type(exc).__name__)
            return SendResult(success=False, error="approval card send failed")
        if not self.book.mark_sent(card.card_id, msg.message_id):
            return SendResult(success=False, error="approval card is no longer live")
        _rec("card_sent", card=card.card_id, tg_message_id=msg.message_id)
        return SendResult(success=True, message_id=card.card_id)

    async def _late(self, card, task):
        """A card that arrived after the deadline: show it is stale at once, drop its buttons."""
        if task.cancelled() or task.exception() is not None:
            _rec("late", card=card.card_id, delivered=False)
            return
        msg = task.result()
        self.book.note_late_delivery(card.card_id, msg.message_id)
        ok = await self._edit_card(card, card.text + "\n\n" + STALE_LINE)
        _rec("late", card=card.card_id, delivered=True, edited=ok)

    async def _edit_card(self, card, text):
        bot = self._bot()
        if bot is None or card.tg_message_id is None:
            return False
        try:
            await bot.edit_message_text(chat_id=self.book.chat, message_id=card.tg_message_id, text=text,
                                        parse_mode="HTML")
            card.final_shown = True
            return True
        except Exception as exc:  # noqa: BLE001 - best effort; the request is settled either way
            _rec("edit_failed", card=card.card_id, exc=type(exc).__name__)
            return False

    async def on_press(self, update, context):
        query = update.callback_query
        parsed = book_mod.parse_callback(getattr(query, "data", "") or "")
        user_id = getattr(getattr(query, "from_user", None), "id", None)
        chat_id = getattr(getattr(getattr(query, "message", None), "chat", None), "id", None)
        if parsed is None:
            await query.answer(TOAST[book_mod.STALE])
            return
        choice, token = parsed
        decision, card = self.book.press(token, choice, user_id, chat_id)
        _rec("press", decision=decision, choice=choice, user=user_id, chat=chat_id,
             card=card.card_id if card else None, home=str(get_hermes_home()))
        if decision != book_mod.RESOLVE:
            alert = decision in (book_mod.WRONG_CHAT, book_mod.NOT_OWNER)
            await query.answer(TOAST[decision], show_alert=alert)
            if (decision == book_mod.STALE and card is not None and card.tg_message_id is not None
                    and not card.final_shown):
                await self._edit_card(card, card.text + "\n\n" + STALE_LINE)
            elif decision == book_mod.STALE and card is None:
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:  # noqa: BLE001
                    pass
            return
        try:
            count = resolve_gateway_approval_card(card.session_key, choice, card.request_id)
        except Exception as exc:  # noqa: BLE001
            _rec("resolve_error", card=card.card_id, exc=type(exc).__name__)
            count = 0
        self.book.finish(card.card_id, choice, count)
        _rec("resolved", card=card.card_id, choice=choice, count=count)
        line = RESULT_LINE[choice] if count else STALE_LINE
        await query.answer(line)
        await self._edit_card(card, card.text + "\n\n" + line)


def _wire(native, adapter):
    """Telegram connect-time factory: remember the app (the owner check happens at send time)."""
    home = str(get_hermes_home())
    accepted = home == STATE["plugin_home"]
    _rec("wire", home=home, adapter_id=id(adapter), app_id=id(native), accepted=accepted)
    if not accepted:  # another profile's bot: never send with it, never take its presses
        return
    STATE["apps"].append((home, native))
    from telegram.ext import CallbackQueryHandler

    async def _on_press(update, context):
        voice = STATE["adapter"]
        if voice is None:
            await update.callback_query.answer(TOAST[book_mod.STALE])
            return
        await voice.on_press(update, context)

    native.add_handler(CallbackQueryHandler(_on_press, pattern=r"^hya:"))


def register(ctx):
    STATE["plugin_home"] = str(get_hermes_home())
    ctx.register_platform(
        name=PLATFORM,
        label="Yandex Alice (S4 probe)",
        adapter_factory=S4Adapter,
        check_fn=lambda: True,
        allowed_users_env="ALICE_OWNER_USER_ID",
        allowlist_suppresses_pairing=True,
        platform_hint="Voice channel probe: answer briefly.",
        display_tier="minimal",
    )
    ctx.register_platform_handler("telegram", _wire)
