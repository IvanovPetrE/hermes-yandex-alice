"""S6b probe plugin = the S6 plugin (``prototypes/s6/plugin``) plus the process id and a file journal in
``_rec``, and ``provides_hooks`` in the manifest. The hook logic is unchanged.

S6 probe plugin: the voice platform's session, cron jobs created by voice, out-of-turn sends.

The model of the stage-3 adapter. Imports only modules listed in Hermes' ``public-api.md``
(``gateway.config``, ``gateway.platform_registry``, ``gateway.platforms.base`` / ``event`` / ``_shared``,
``gateway.session_context``, ``hermes_constants``). It never reads ``os.environ``; secrets and the chat come
from ``get_scoped_secret``. There is no HTTP listener: S5 proved it, the harness injects turns directly.

* ``send()`` classifies every send (``recovery.classify_send``); a send OUTSIDE any open turn that is
  neither service nor a recovered reply goes to Telegram as ``Алиса:\\n\\n<text>`` - the default profile's
  bot, its own ``PlatformConfig`` built in ``connect()``, a task in a copy of the ``connect()`` context (S3).
* ``pre_tool_call`` hook: in a voice session (``HERMES_SESSION_PLATFORM == yandex_alice``) the
  ``cronjob_manage`` arguments get ``cron_guard.guard_args`` merged in. The hook never raises.
* ``standalone_sender_fn`` only records that cron tried it (and refuses): S6 asks whether cron ever does.

``JOURNAL`` is for the probe harness: metadata, lengths and the first characters of SYNTHETIC scripted text.
A plugin loaded in two homes is two modules (``hermes_plugins.<slug>`` and ``…__home_<digest>``), each with its
own ``JOURNAL``; the harness reads both.
"""

import asyncio
import contextvars
import json
import logging
import os
import time

from gateway.config import Platform, PlatformConfig
from gateway.platform_registry import platform_registry
from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session_context import get_session_env
from hermes_constants import get_hermes_home

from . import cron_guard, recovery

PLATFORM = "yandex_alice"
OWNER_ID = "u-probe-owner"
TELEGRAM_EXTRA_KEYS = {"telegram_base_url": "base_url", "telegram_proxy_url": "proxy_url"}
FORWARD_DEADLINE_S = 60.0
HINT = ("Voice channel probe: answer briefly. Cron jobs and reminders created here must always be "
        "delivered to Telegram: set deliver=telegram:<the configured chat>.")

logger = logging.getLogger(__name__)
JOURNAL: list = []
STATE: dict = {"home": None}


def _rec(op, **kw):
    """S6b: the record carries the process id and goes ALSO to a file in the home, line by line: a journal
    kept in the memory of another process (a managed worker) would be invisible to the harness."""
    row = dict(t=time.monotonic(), op=op, home=str(get_hermes_home()), pid=os.getpid(), **kw)
    JOURNAL.append(row)
    try:
        with (get_hermes_home() / "s6b-journal.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(row, t=time.time()), ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 - the journal never breaks the plugin
        pass


def _secret(name):
    try:
        return (get_scoped_secret(name, "") or "").strip()
    except Exception as exc:  # noqa: BLE001 - an unscoped read: report the class only
        return "!" + type(exc).__name__


class Forwarder:
    """Telegram sender for out-of-turn sends (the S3 path); everything scope-dependent is read in connect()."""

    def __init__(self):
        self.enabled = False
        self.reason = "not_connected"
        self.pconfig = self.sender = self.chat = self.ctx = None

    def connect(self, plugin_extra):
        token, chat = _secret("TELEGRAM_BOT_TOKEN"), _secret("ALICE_TELEGRAM_CHAT")
        entry = platform_registry.get("telegram")
        sender = getattr(entry, "standalone_sender_fn", None)
        bad = lambda v: not v or v.startswith("!")  # noqa: E731
        self.reason = ("no_token" if bad(token) else "no_chat" if bad(chat) else
                       "no_sender" if sender is None else None)
        self.enabled = self.reason is None
        if not self.enabled:
            self.pconfig = self.sender = None
            return
        extra = {dst: plugin_extra[src] for src, dst in TELEGRAM_EXTRA_KEYS.items() if plugin_extra.get(src)}
        self.pconfig = PlatformConfig(enabled=True, token=token, extra=extra)
        self.sender, self.chat = sender, chat
        self.ctx = contextvars.copy_context()

    def forward(self, text):
        loop = asyncio.get_running_loop()
        if not self.enabled:
            return loop.create_task(self._disabled())
        return loop.create_task(self._send(text), context=self.ctx.copy())

    async def _disabled(self):
        return {"status": "disabled", "reason": self.reason}

    async def _send(self, text):
        try:
            result = await asyncio.wait_for(self.sender(self.pconfig, self.chat, text, notify=True),
                                            FORWARD_DEADLINE_S)
        except asyncio.TimeoutError:
            return {"status": "uncertain", "reason": "deadline"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "uncertain", "reason": "raised", "exc": type(exc).__name__}
        if isinstance(result, dict) and result.get("success"):
            return {"status": "delivered", "sender_home": str(get_hermes_home())}
        if isinstance(result, dict) and result.get("error"):
            return {"status": "failed", "error": str(result.get("error"))[:120]}
        return {"status": "uncertain", "reason": "unexpected_result"}


class AliceS6(BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self, config):
        super().__init__(config, Platform(PLATFORM))
        self._extra = dict(getattr(config, "extra", None) or {})
        self._open = {}  # chat -> message id of the open turn
        self._n = 0
        self.forwarder = Forwarder()
        self._tasks = set()
        _rec("create", adapter_id=id(self))

    async def connect(self, *, is_reconnect=False):
        self.forwarder.connect(self._extra)
        _rec("connect", forward_enabled=self.forwarder.enabled, reason=self.forwarder.reason,
             is_reconnect=is_reconnect)
        self._running = True
        self._mark_connected()
        return True

    async def disconnect(self):
        self._running = False
        self._mark_disconnected()
        _rec("disconnect")

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self._n += 1
        meta = dict(metadata or {})
        chat = str(chat_id)
        text = content or ""
        reply = None if reply_to is None else str(reply_to)
        open_turn = chat in self._open
        kind = recovery.classify_send(open_turn=open_turn, turn_mid=self._open.get(chat), reply_to=reply,
                                      notify=meta.get("notify"), interim=bool(meta.get("_interim_send")), text=text)
        forward = recovery.forward_text(open_turn=open_turn, kind=kind, text=text)
        _rec("send", chat=chat, kind=kind, open_turn=open_turn, turn_mid=self._open.get(chat), reply_to=reply,
             notify=meta.get("notify"), interim=bool(meta.get("_interim_send")), meta_keys=sorted(meta),
             length=len(text), head=text[:100], forwarded=forward is not None)
        if forward is not None:
            task = self.forwarder.forward(forward)
            self._tasks.add(task)
            task.add_done_callback(lambda t, c=chat: self._forwarded(t, c))
        return SendResult(success=True, message_id="out-%d" % self._n)

    def _forwarded(self, task, chat):
        self._tasks.discard(task)
        result = {"status": "cancelled"} if task.cancelled() else (
            {"status": "uncertain", "exc": type(task.exception()).__name__} if task.exception() else task.result())
        _rec("forward", chat=chat, **result)

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        _rec("clarify", chat=str(chat_id), choices=len(choices or []), length=len(question or ""))
        return await super().send_clarify(chat_id, question, choices, clarify_id, session_key, metadata=metadata)

    async def on_processing_start(self, event):
        chat = str(event.source.chat_id)
        self._open[chat] = event.message_id
        _rec("start", chat=chat, mid=event.message_id, internal=bool(getattr(event, "internal", False)),
             text_len=len(event.text or ""), profile=getattr(event.source, "profile", None))

    async def on_processing_complete(self, event, outcome):
        chat = str(event.source.chat_id)
        self._open.pop(chat, None)
        _rec("complete", chat=chat, mid=event.message_id, internal=bool(getattr(event, "internal", False)),
             outcome=getattr(outcome, "value", str(outcome)))

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        return SendResult(success=False, error="Not supported")

    async def delete_message(self, chat_id, message_id):
        return False

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"name": "chat-%s" % chat_id, "type": "dm", "chat_id": chat_id}

    async def probe_inject(self, text, message_id, chat_id):
        source = self.build_source(chat_id=chat_id, chat_name="chat-%s" % chat_id, chat_type="dm",
                                   user_id=OWNER_ID, user_name="probe")
        await self.handle_message(MessageEvent(text=text, message_type=MessageType.TEXT,
                                               source=source, message_id=message_id))


def _shape(value):
    """What a deliver value looks like, without the value: type and the platform part of each token."""
    if value is None:
        return None
    if isinstance(value, str):
        return [t.strip().split(":", 1)[0] for t in value.split(",") if t.strip()]
    return type(value).__name__


def _cron_hook(tool_name=None, args=None, task_id=None, **kwargs):
    """``pre_tool_call``: keep a voice-created cron job off the voice channel. Never raises."""
    try:
        if tool_name != cron_guard.TOOL:
            return None
        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        chat = _secret("ALICE_TELEGRAM_CHAT")
        merged = None
        if platform == PLATFORM:
            merged = cron_guard.guard_args(args, None if chat.startswith("!") else chat)
        a = args if isinstance(args, dict) else {}
        _rec("hook", platform=platform, chat_set=bool(chat) and not chat.startswith("!"), chat_err=chat[1:] if
             chat.startswith("!") else None, action=a.get("action"), deliver=_shape(a.get("deliver")),
             failure_deliver=_shape(a.get("failure_deliver")), merged=sorted(merged or {}),
             kwargs=sorted(kwargs))
        return {"action": "modify", "args": merged} if merged else None
    except Exception as exc:  # noqa: BLE001 - a raising pre_tool_call hook blocks every tool of the profile
        try:
            _rec("hook_error", exc=type(exc).__name__)
        except Exception:  # noqa: BLE001
            pass
        return None


async def _standalone(pconfig, chat_id, message, **kwargs):
    """Records that cron tried the out-of-process path for ``yandex_alice``; never delivers."""
    _rec("standalone", chat=str(chat_id), length=len(message or ""), kwargs=sorted(kwargs))
    return {"error": "S6 probe: the voice channel cannot speak first"}


def _validate(config):
    """The secret must exist in the CURRENT scope: a profile without it never enables the platform (S2)."""
    return bool(_secret("ALICE_PATH_SECRET")) and not _secret("ALICE_PATH_SECRET").startswith("!")


def register(ctx):
    STATE["home"] = str(get_hermes_home())
    _rec("register")
    ctx.register_platform(
        name=PLATFORM,
        label="Yandex Alice (S6b probe)",
        adapter_factory=AliceS6,
        check_fn=lambda: True,
        validate_config=_validate,
        allowed_users_env="ALICE_OWNER_USER_ID",
        allowlist_suppresses_pairing=True,
        platform_hint=HINT,
        display_tier="minimal",
        standalone_sender_fn=_standalone,
    )
    ctx.register_hook("pre_tool_call", _cron_hook)
