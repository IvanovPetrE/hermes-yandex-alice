"""Подтверждения опасных команд голосового хода через карточку в Telegram (docs/PLAN.md, этап 5, срез «б»;
docs/RECON.md, «S4») — только стандартная библиотека и asyncio, без Hermes и PTB.
"""
from __future__ import annotations

import asyncio
import html
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

HEADER = "⚠️ <b>Алиса: подтвердите команду</b>"   # решение владельца 2026-11-17
MAX_CMD_ON_CARD = 3500   # экранированных символов команды, которые карточка показывает целиком
PREVIEW_CHARS = 3000
REASON_CHARS = 500

RESULT_LINE = {"once": "✅ Разрешено один раз", "deny": "❌ Отклонено"}
STALE_LINE = "⌛ Запрос неактуален"
TIMEOUT_LINE = "⌛ Время вышло — команда не выполнена."   # вместо английского текста Hermes (решение владельца)
FINAL_LINES = (*RESULT_LINE.values(), STALE_LINE, TIMEOUT_LINE)   # строка итога под карточкой


CHOICES = ("once", "deny")   # только одноразовое разрешение: без session / always
LABELS = {"once": "Разрешить один раз", "deny": "Отклонить"}
TELEGRAM_LIMIT = 4096   # единиц UTF-16 в тексте сообщения


def parse_id(raw: object) -> int | None:
    text = str(raw or "").strip()
    try:
        return int(text) if text else None
    except ValueError:
        return None


def resolve_owner(chat_raw: object, owner_raw: object) -> tuple[int | None, int | None]:
    """`(чат, владелец)`; владелец None — подтверждения отключены: его нельзя определить."""
    chat = parse_id(chat_raw)
    if chat is None:
        return None, None
    if str(owner_raw or "").strip():
        return chat, parse_id(owner_raw)
    return chat, (chat if chat > 0 else None)


PREFIX = "hya"


def callback_data(choice: str, token: str) -> str:
    return f"{PREFIX}:{choice}:{token}"


def parse_callback(data: object) -> tuple[str, str] | None:
    """`(выбор, токен)` нашей кнопки, иначе None."""
    parts = str(data or "").split(":")
    if len(parts) != 3 or parts[0] != PREFIX or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


FULL_COMMAND_MIN = 3000   # `_EA_CMD_BUDGET` Hermes: длиннее — сначала блок всей команды


def is_full_command_block(content: str, metadata: dict | None) -> bool:
    """Вся команда, которую Hermes шлёт через голосовой `send()` перед урезанной карточкой: промежуточная отправка
    блоком ```` ``` ```` длиннее бюджета карточки (по форме; с командой ожидающего запроса сверить нечем — законный
    промежуточный блок такой же формы тоже получит отказ, он и так не озвучивается)."""
    text = content or ""
    return (bool((metadata or {}).get("_interim_send")) and text.startswith("```\n")
            and text.endswith("\n```") and len(text) > FULL_COMMAND_MIN)


def offered_choices(actions: list, whole: bool) -> tuple[str, ...]:
    """Кнопки карточки: строки Hermes `(подпись, выбор, стиль)`, урезанные до once/deny; «Отклонить» — всегда;
    команда не целиком — только «Отклонить»."""
    offered = {c for _label, c, _style in actions or [] if c in CHOICES and (whole or c == "deny")}
    offered.add("deny")
    return tuple(c for c in CHOICES if c in offered)


def units(text: str) -> int:
    """Длина в единицах UTF-16 — так считает Telegram."""
    return len(text.encode("utf-16-le")) // 2


def _fit(raw: str, room: int) -> str:
    """Самое длинное начало `raw`, которое после HTML-экранирования занимает не больше `room` единиц."""
    total = 0
    for i, ch in enumerate(raw):
        total += units(html.escape(ch))
        if total > room:
            return raw[:i]
    return raw


def _compose(shown: str, reason: str, whole: bool, smart_denied: bool) -> str:
    lines = [HEADER, f"<pre>{shown}</pre>", "Причина: " + reason]
    if not whole:
        lines.append("Команда показана не целиком — разрешить нельзя, только отклонить.")
    if smart_denied:
        lines.append("Модель безопасности Hermes советует отклонить.")
    lines.append("Без ответа команда не выполнится.")
    return "\n".join(lines)


def render_card(command: str, description: str, smart_denied: bool) -> tuple[str, bool]:
    """`(HTML-текст, целиком ли команда)`. Бюджет — по готовому тексту: карточка и любая строка итога под ней
    (`FINAL_LINES`) укладываются в лимит Telegram; длинная причина уступает первой, команда не целиком — «только
    отклонить»."""
    cmd, reason = command or "", (description or "")[:REASON_CHARS]
    room = TELEGRAM_LIMIT - max(units("\n\n" + line) for line in FINAL_LINES)
    whole = units(html.escape(cmd)) <= MAX_CMD_ON_CARD
    if whole:
        shown = html.escape(cmd)
        reason = _fit(reason, room - units(_compose(shown, "", whole, smart_denied)))
    else:
        reason = _fit(reason, room - units(_compose("…", "", whole, smart_denied)) - PREVIEW_CHARS)
        shown = html.escape(_fit(cmd, room - units(_compose("…", html.escape(reason), whole, smart_denied)))) + "…"
    return _compose(shown, html.escape(reason), whole, smart_denied), whole


# --- книга карточек ------------------------------------------------------------------------------------------------

LIVE = ("sending", "open", "resolving")

# Решение по нажатию.
RESOLVE = "resolve"
STALE = "stale"             # токен неизвестен или карточка уже не живая
WAIT = "wait"               # Telegram ещё не подтвердил отправку: нажать через секунду
WRONG_CHAT = "wrong_chat"
NOT_OWNER = "not_owner"
NOT_OFFERED = "not_offered"


@dataclass
class Card:
    card_id: str
    token: str
    session_key: str
    request_id: str
    alice_chat: str
    choices: tuple[str, ...]
    text: str
    created_at: float
    state: str = "sending"
    tg_message_id: int | None = None
    final_shown: bool = False   # на карточке уже строка итога, кнопок нет


class Book:
    """Одна карточка на запрос подтверждения Hermes. Настройки (чат, владелец) — не здесь: книга переживает
    повторный `connect()` (ревью S4, п. 10)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 new_token: Callable[[], str] = lambda: secrets.token_urlsafe(12),
                 new_card_id: Callable[[], str] = lambda: "hya-card:" + secrets.token_hex(6),
                 keep_seconds: float = 86400.0) -> None:
        self._clock, self._new_token, self._new_card_id = clock, new_token, new_card_id
        self._keep = keep_seconds   # завершённые карточки помнятся сутки — для «неактуален», потом забываются
        self._lock = threading.Lock()
        self._by_token: dict[str, Card] = {}
        self._by_id: dict[str, Card] = {}

    def open(self, session_key: str, request_id: str | None, alice_chat: str, choices: tuple[str, ...],
             text: str) -> Card | None:
        """Новая карточка в `sending`; None без `request_id` или `session_key`."""
        if not request_id or not session_key:
            return None
        card = Card(self._new_card_id(), self._new_token(), session_key, str(request_id), str(alice_chat),
                    tuple(choices), text, self._clock())
        with self._lock:
            for old in [c for c in self._by_id.values()
                        if c.state not in LIVE and card.created_at - c.created_at >= self._keep]:
                del self._by_id[old.card_id], self._by_token[old.token]
            self._by_token[card.token] = card
            self._by_id[card.card_id] = card
        return card

    def _move(self, card: Card, frm: tuple[str, ...], to: str) -> bool:
        if card.state not in frm:
            return False
        card.state = to
        return True

    def mark_sent(self, card_id: str, tg_message_id: int) -> bool:
        """sending → open. False: срок уже провалил карточку (поздняя доставка)."""
        with self._lock:
            card = self._by_id.get(card_id)
            if card is None:
                return False
            card.tg_message_id = tg_message_id
            return self._move(card, ("sending",), "open")

    def claim_final(self, card_id: str) -> bool:
        """Право показать строку итога — один раз на карточку; живую (`sending`/`open`/`resolving`) не трогает."""
        with self._lock:
            card = self._by_id.get(card_id)
            if card is None or card.final_shown or card.state in LIVE:
                return False
            card.final_shown = True
            return True

    def fail(self, card_id: str) -> bool:
        """sending → failed. True — вызывающий обязан отказать в запросе сейчас."""
        with self._lock:
            card = self._by_id.get(card_id)
            return card is not None and self._move(card, ("sending",), "failed")

    def finish(self, card_id: str, count: int) -> Card | None:
        """resolving → resolved (`count` > 0) или expired (запрос уже решён иначе: «уже решено», не сбой)."""
        with self._lock:
            card = self._by_id.get(card_id)
            if card is not None:
                self._move(card, ("resolving",), "resolved" if count else "expired")
            return card

    def expire(self, card_id: str) -> Card | None:
        """open → expired (таймаут Hermes правит карточку). None — карточка не открыта."""
        with self._lock:
            card = self._by_id.get(card_id)
            return card if card is not None and self._move(card, ("open",), "expired") else None

    def close_chat(self, alice_chat: str) -> list[Card]:
        """Ход окончен: открытые карточки беседы больше ничего не разрешат."""
        with self._lock:
            return [c for c in self._by_id.values()
                    if c.alice_chat == str(alice_chat) and self._move(c, ("open",), "expired")]

    def press(self, token: str, choice: str, user_id: object, chat_id: object, *, chat: int | None,
              owner: int | None) -> tuple[str, Card | None]:
        """Одно нажатие. Порядок: токен → чат → владелец → состояние → выбор; постороннему состояние карточки не
        видно. `RESOLVE` переводит карточку в `resolving` — побеждает ровно одно нажатие."""
        with self._lock:
            card = self._by_token.get(token)
            if card is None:
                return STALE, None
            if chat is None or parse_id(chat_id) != chat:
                return WRONG_CHAT, card
            if owner is None or parse_id(user_id) != owner:
                return NOT_OWNER, card
            if card.state == "sending":
                return WAIT, card
            if card.state != "open":
                return STALE, card
            if choice not in card.choices:
                return NOT_OFFERED, card
            self._move(card, ("open",), "resolving")
            return RESOLVE, card

    def awaiting(self, alice_chat: str) -> bool:
        with self._lock:
            return any(c.alice_chat == str(alice_chat) and c.state in LIVE for c in self._by_id.values())


# --- оркестратор ---------------------------------------------------------------------------------------------------

DENY_FAILED = ("Автоматический отказ плагина: карточка подтверждения не доставлена в Telegram, владелец её не видел")
DENY_OFF = "Автоматический отказ плагина: подтверждения в Telegram не настроены, владелец запрос не видел"
FAILED, OFF = "failed", "off"   # метка хода для итога: своя озвучка (dialog.Final)
TOAST = {
    WAIT: "Подождите секунду и нажмите снова",
    STALE: "Запрос неактуален",
    WRONG_CHAT: "Отвечать может только владелец",
    NOT_OWNER: "Отвечать может только владелец",
    NOT_OFFERED: "Этот выбор недоступен",
}


@dataclass(frozen=True)
class PressReply:
    """Ответ на нажатие: всплывающий текст; `alert` — окном; `strip` — снять кнопки с сообщения нажатия (карточка
    неизвестна книге: прошлый процесс шлюза)."""
    toast: str
    alert: bool = False
    strip: bool = False


class Approvals:
    """Карточки подтверждения голосового хода (S4, «Выводы»). Наружу — только параметрами: `deny(session_key,
    request_id, reason) -> int` и `resolve(session_key, choice, request_id) -> int` (Hermes, `tools.approval`), `bot()` —
    бот профиля по умолчанию или None (`send(chat, text, buttons) -> message_id`, `edit(chat, message_id, text)`),
    `notify(беседа, ждёт ли)` — флаг «ждёт подтверждения» машины беседы. Один объект на регистрацию плагина: книга
    переживает переподключение адаптера, настройки — `configure()` на каждом `connect()`. В лог — только `request_id`,
    исход и длины: ни команды, ни причины."""

    def __init__(self, deny: Callable[[str, str, str], int], resolve: Callable[[str, str, str], int], *,
                 bot: Callable[[], object | None] = lambda: None, deadline: float = 10.0, book: Book | None = None,
                 log: Callable[[str], None] | None = None) -> None:
        self._deny, self._resolve, self.bot, self._deadline = deny, resolve, bot, deadline
        self.book = book if book is not None else Book()
        self._log = log or logger.info
        self.chat: int | None = None
        self.owner: int | None = None
        self._enabled = False
        self._notify: Callable[[str, bool], None] = lambda chat, waiting: None
        self._outcome: dict[str, str] = {}   # беседа -> метка текущего хода
        self._tasks: set[asyncio.Task] = set()

    def configure(self, chat_raw: object, owner_raw: object, *, enabled: bool,
                  notify: Callable[[str, bool], None] | None = None) -> None:
        """`connect()`: чат и владелец (`resolve_owner`); `enabled` — Telegram включён. Живые карточки остаются."""
        self.chat, self.owner = resolve_owner(chat_raw, owner_raw)
        self._enabled = enabled
        if notify is not None:
            self._notify = notify

    @property
    def enabled(self) -> bool:
        return self._enabled and self.chat is not None and self.owner is not None

    def awaiting(self, alice_chat: str) -> bool:
        return self.book.awaiting(alice_chat)

    def outcome(self, alice_chat: str) -> str:
        """Метка хода беседы: "" — сбоев не было, `failed` — карточка не дошла, `off` — подтверждения отключены."""
        return self._outcome.get(str(alice_chat), "")

    def _refuse(self, session_key: str, request_id: str, reason: str) -> None:
        try:
            count = self._deny(session_key, request_id, reason)
        except Exception as exc:  # noqa: BLE001 - запрос дождётся таймаута Hermes: тоже отказ
            self._log(f"approval deny raised: request_id={request_id} error={type(exc).__name__}")
            return
        self._log(f"approval denied by plugin: request_id={request_id} count={count}")

    async def prompt(self, *, alice_chat: str, session_key: str, request_id: str | None, command: str,
                     description: str, smart_denied: bool, actions: list) -> str | None:
        """`_send_exec_approval_prompt`: id карточки, если она дошла; иначе None — запрос уже отклонён."""
        alice_chat = str(alice_chat)
        if not self.enabled:
            self._outcome[alice_chat] = OFF
            if request_id and session_key:
                self._refuse(session_key, request_id, DENY_OFF)
            self._log(f"approval refused: request_id={request_id} outcome=off")
            return None
        text, whole = render_card(command, description, smart_denied)
        card = self.book.open(session_key, request_id, alice_chat, offered_choices(actions, whole), text)
        if card is None:   # без request_id отказать нельзя: resolve_* ответил бы на самый старый запрос сессии
            self._outcome[alice_chat] = FAILED
            self._log(f"approval card not opened: request_id={request_id}")
            return None
        self._notify(alice_chat, True)
        bot = self.bot()
        if bot is None:
            self._failed(card, "no_bot")
            return None
        buttons = tuple((LABELS[c], callback_data(c, card.token)) for c in card.choices)
        send = asyncio.ensure_future(bot.send(self.chat, text, buttons))
        try:
            message_id = await asyncio.wait_for(asyncio.shield(send), self._deadline)
        except asyncio.TimeoutError:
            self._failed(card, "deadline")
            self._spawn(self._late(card, send))
            return None
        except asyncio.CancelledError:   # ход отменили во время отправки: отказ, отмена идёт дальше
            self._failed(card, "cancelled")
            self._spawn(self._late(card, send))
            raise
        except Exception as exc:  # noqa: BLE001 - любой сбой отправки — отказ; текст исключения в лог не пишем
            self._failed(card, type(exc).__name__)
            return None
        self.book.mark_sent(card.card_id, message_id)
        self._log(f"approval card sent: request_id={card.request_id} length={len(text)}")
        return card.card_id

    async def press(self, data: object, user_id: object, chat_id: object) -> PressReply:
        """Нажатие кнопки `hya:`: проверки книги → `resolve` по `request_id` карточки (без `actor_id`: кто нажимает —
        проверка плагина, S4, вывод 2) → строка итога на карточке."""
        parsed = parse_callback(data)
        if parsed is None:
            return PressReply(TOAST[STALE])
        choice, token = parsed
        decision, card = self.book.press(token, choice, user_id, chat_id, chat=self.chat, owner=self.owner)
        if decision == STALE and card is not None:   # не живая, итога на ней нет — снять кнопки (`_show` не тронет
            await self._show(card, STALE_LINE)        # `resolving` и уже показанный итог)
        if decision == STALE and card is None:   # карточка прошлого процесса: кнопки — с сообщения нажатия
            return PressReply(TOAST[STALE], strip=self.chat is not None and parse_id(chat_id) == self.chat)
        if decision != RESOLVE:
            return PressReply(TOAST[decision], alert=decision in (WRONG_CHAT, NOT_OWNER))
        try:
            count = self._resolve(card.session_key, choice, card.request_id)
        except Exception as exc:  # noqa: BLE001 - не разрешили — значит, ничего не выполнено
            self._log(f"approval resolve raised: request_id={card.request_id} error={type(exc).__name__}")
            count = 0
        self.book.finish(card.card_id, count)
        self._log(f"approval pressed: request_id={card.request_id} choice={choice} count={count}")
        self._notify(card.alice_chat, self.book.awaiting(card.alice_chat))
        line = RESULT_LINE[choice] if count else STALE_LINE
        await self._show(card, line)
        return PressReply(line)

    async def timed_out(self, card_id: str) -> bool:
        """`edit_message` адаптера с id карточки — правка Hermes на таймауте подтверждения: своя строка вместо его
        английского текста, кнопки сняты. False — карточка не открыта (уже решена или неизвестна)."""
        card = self.book.expire(str(card_id))
        if card is None:
            return False
        self._log(f"approval timed out: request_id={card.request_id}")
        self._notify(card.alice_chat, self.book.awaiting(card.alice_chat))
        return await self._show(card, TIMEOUT_LINE)

    def start_turn(self, alice_chat: str) -> None:
        """`on_processing_start`: метка прошлого хода не должна достаться итогу нового."""
        self._outcome.pop(str(alice_chat), None)

    async def turn_closed(self, alice_chat: str) -> None:
        """`on_processing_complete`: открытые карточки беседы больше ничего не разрешат — сразу «неактуален»
        (Hermes правит карточку только на своём таймауте и только пока ход текущий)."""
        closed = self.book.close_chat(str(alice_chat))
        self._notify(str(alice_chat), self.book.awaiting(str(alice_chat)))
        for card in closed:
            await self._show(card, STALE_LINE)

    def _spawn(self, coro) -> None:
        """Фоновая задача с сильной ссылкой (ревью S4, п. 10): иначе сборщик мусора может её потерять."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _late(self, card: Card, send: asyncio.Future) -> None:
        """Карточка дошла после срока (запрос уже отклонён): сразу «неактуален», кнопки снимаются."""
        try:
            message_id = await send
        except BaseException as exc:  # noqa: BLE001 - не дошла вовсе: снимать нечего
            self._log(f"approval late send gave up: request_id={card.request_id} error={type(exc).__name__}")
            return
        self.book.mark_sent(card.card_id, message_id)   # False: карточка уже failed, но message_id известен
        await self._show(card, STALE_LINE)

    async def _show(self, card: Card, line: str) -> bool:
        """Строка итога под карточкой, кнопки сняты; у карточки — один раз. Сбой правки запрос не меняет."""
        bot = self.bot()
        if bot is None or card.tg_message_id is None or not self.book.claim_final(card.card_id):
            return False
        try:
            await bot.edit(self.chat, card.tg_message_id, card.text + "\n\n" + line)
            return True
        except Exception as exc:  # noqa: BLE001
            self._log(f"approval card edit failed: request_id={card.request_id} error={type(exc).__name__}")
            return False

    def _failed(self, card: Card, why: str) -> None:
        """Карточка не дошла: отказ по её `request_id` (если срок или сбой не опередили друг друга), метка хода."""
        if self.book.fail(card.card_id):
            self._outcome[card.alice_chat] = FAILED
            self._refuse(card.session_key, card.request_id, DENY_FAILED)
            self._log(f"approval card failed: request_id={card.request_id} why={why}")
        self._notify(card.alice_chat, self.book.awaiting(card.alice_chat))
