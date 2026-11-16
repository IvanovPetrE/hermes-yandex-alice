"""S5 probe plugin: lifecycle of a loopback-listening platform adapter and restart recovery.

The model of the stage-3 adapter. Imports only modules listed in Hermes' ``public-api.md``
(``gateway.config``, ``gateway.platforms.base`` / ``event`` / ``_shared``, ``hermes_constants``) and
``aiohttp``. It never reads ``os.environ`` (``os.getpid`` only): the port and the flags come from
``platforms.yandex_alice.extra``, the secret path from ``get_scoped_secret``.

* ``register(ctx)`` only registers; no socket is opened before ``connect()``.
* ``connect()`` binds ``127.0.0.1:<port>`` (``AppRunner(access_log=None)`` + ``TCPSite``). A taken port
  is a RETRYABLE fatal error (``bind_failed``): no other port, no stolen socket, no path or body in logs.
* ``disconnect()`` stops the site and cleans the runner.
* ``POST /<secret>`` with ``{"text", "mid", "chat"}`` -> ``handle_message``.
* ``send()`` classifies every send with ``recovery.classify_send`` and journals it (metadata only).
  ``interactive_resume`` comes from ``extra`` (Hermes reads it from the adapter).
* ``s5_other`` is a second platform without a port: it proves the rest of the gateway keeps working
  while ``yandex_alice`` cannot bind.

``JOURNAL`` lines go to ``<home>/s5-journal.jsonl`` for the harness: lengths, ids, flags and the
first characters of SYNTHETIC scripted text; never the secret path.
"""

import asyncio
import contextlib
import hmac
import json
import logging
import os
import time
from pathlib import Path

from gateway.config import Platform
from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from hermes_constants import get_hermes_home

from . import recovery

PLATFORM = "yandex_alice"
OTHER = "s5_other"
OWNER_ID = "u-probe-owner"
LOADED_AT = time.monotonic()  # a re-import of the module (reload) shows up as a new value
STATE = {"home": None, "register_calls": 0}
logger = logging.getLogger(__name__)


def _rec(op, **kw):
    home = STATE["home"]
    if home is None:
        return
    row = dict(t=round(time.monotonic(), 3), pid=os.getpid(), op=op, **kw)
    try:
        with (home / "s5-journal.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def _extra(config):
    return dict(getattr(config, "extra", None) or {})


class AliceS5(BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self, config):
        super().__init__(config, Platform(PLATFORM))
        extra = _extra(config)
        self.port = int(extra["port"])
        self.interactive_resume = bool(extra.get("interactive_resume", True))
        self._runner = None
        self._site = None
        self._secret = ""
        self._open = {}  # chat -> message id of the open turn
        self._n = 0
        _rec("create", adapter_id=id(self), port=self.port, interactive_resume=self.interactive_resume)

    # -- lifecycle --

    def _fail(self, code, detail, *, retryable):
        self._set_fatal_error(code, detail, retryable=retryable)
        return False

    async def connect(self, *, is_reconnect=False):
        self._secret = (get_scoped_secret("ALICE_PATH_SECRET", "") or "").strip()
        if not self._secret:
            _rec("connect", ok=False, why="no_secret", is_reconnect=is_reconnect, adapter_id=id(self))
            return self._fail("config_missing", "ALICE_PATH_SECRET is not set", retryable=False)
        from aiohttp import web

        app = web.Application(client_max_size=16 * 1024)
        app.router.add_post("/{tail:.*}", self._handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", self.port)
        try:
            await site.start()
        except OSError as exc:
            with contextlib.suppress(Exception):
                await runner.cleanup()
            _rec("connect", ok=False, why="bind", errno=exc.errno, is_reconnect=is_reconnect, adapter_id=id(self))
            # No path, no text, no exception message: the port number is configuration, not a secret.
            return self._fail("bind_failed", "could not bind the loopback port", retryable=True)
        self._runner, self._site = runner, site
        self._running = True
        self._mark_connected()
        _rec("connect", ok=True, is_reconnect=is_reconnect, adapter_id=id(self))
        return True

    async def disconnect(self):
        self._running = False
        self._mark_disconnected()
        for obj, method in ((self._site, "stop"), (self._runner, "cleanup")):
            if obj is not None:
                with contextlib.suppress(Exception):
                    await getattr(obj, method)()
        self._site = self._runner = None
        _rec("disconnect", adapter_id=id(self))

    def rewire_plugin_handlers(self):
        _rec("rewire", adapter_id=id(self))
        return super().rewire_plugin_handlers()

    # -- inbound --

    async def _handle(self, request):
        from aiohttp import web

        if not hmac.compare_digest(request.match_info["tail"].encode("utf-8"), self._secret.encode("utf-8")):
            _rec("http", ok=False, status=404)
            return web.Response(status=404)
        try:
            body = await request.json()
            text, mid, chat = str(body["text"]), str(body["mid"]), str(body.get("chat", "chat-1"))
        except Exception:  # noqa: BLE001 - any malformed body is a 400, never logged
            _rec("http", ok=False, status=400)
            return web.Response(status=400)
        t0 = time.monotonic()
        source = self.build_source(chat_id=chat, chat_name="chat-%s" % chat, chat_type="dm",
                                   user_id=OWNER_ID, user_name="probe")
        try:
            await self.handle_message(MessageEvent(text=text, message_type=MessageType.TEXT,
                                                   source=source, message_id=mid))
        except Exception as exc:  # noqa: BLE001
            _rec("http", ok=True, status=500, mid=mid, exc=type(exc).__name__)
            return web.Response(status=500)
        _rec("http", ok=True, status=200, mid=mid, chat=chat, took=round(time.monotonic() - t0, 3))
        return web.json_response({"accepted": True})

    # -- outbound / hooks --

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self._n += 1
        meta = dict(metadata or {})
        chat = str(chat_id)
        text = content or ""
        reply = None if reply_to is None else str(reply_to)
        kind = recovery.classify_send(
            open_turn=chat in self._open, turn_mid=self._open.get(chat), reply_to=reply,
            notify=meta.get("notify"), interim=bool(meta.get("_interim_send")), text=text)
        _rec("send", chat=chat, kind=kind, open_turn=chat in self._open, turn_mid=self._open.get(chat),
             reply_to=reply, notify=meta.get("notify"), interim=bool(meta.get("_interim_send")),
             meta_keys=sorted(meta), thread_id=meta.get("thread_id"),
             approval_prompt=bool(meta.get("is_approval_prompt")), length=len(text), head=text[:80],
             marker=bool(recovery.split_marker(text)[0]), after_strip=recovery.strip_marker(text)[:40])
        control = STATE["home"] / "s5-control"
        if kind == recovery.FINAL and (control / "hold_final").exists():
            _rec("held", chat=chat)  # the harness kills the gateway here: a crash inside the final send
            await asyncio.sleep(3600)
        return SendResult(success=True, message_id="out-%d" % self._n)

    async def _send_exec_approval_prompt(self, prompt):
        """Taking the button path keeps the approval in the gateway queue; there is no card to press."""
        self._n += 1
        _rec("approval_prompt", chat=str(prompt.chat_id), request_id=prompt.request_id,
             command_len=len(prompt.command or ""))
        return SendResult(success=True, message_id="card-%d" % self._n)

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


class OtherS5(BasePlatformAdapter):
    """A platform without a port: a turn on it must work while ``yandex_alice`` cannot bind."""

    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self, config):
        super().__init__(config, Platform(OTHER))
        self._n = 0

    async def connect(self, *, is_reconnect=False):
        self._running = True
        self._mark_connected()
        _rec("other_connect", is_reconnect=is_reconnect)
        return True

    async def disconnect(self):
        self._running = False
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self._n += 1
        meta = dict(metadata or {})
        _rec("other_send", chat=str(chat_id), reply_to=None if reply_to is None else str(reply_to),
             notify=meta.get("notify"), head=(content or "")[:60])
        return SendResult(success=True, message_id="other-%d" % self._n)

    async def on_processing_complete(self, event, outcome):
        _rec("other_complete", mid=event.message_id, outcome=getattr(outcome, "value", str(outcome)))

    async def get_chat_info(self, chat_id):
        return {"name": "chat-%s" % chat_id, "type": "dm", "chat_id": chat_id}

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def probe_inject(self, text, message_id, chat_id):
        source = self.build_source(chat_id=chat_id, chat_name="chat-%s" % chat_id, chat_type="dm",
                                   user_id=OWNER_ID, user_name="probe")
        await self.handle_message(MessageEvent(text=text, message_type=MessageType.TEXT,
                                               source=source, message_id=message_id))


def _validate(config):
    """The secret must exist in the CURRENT scope: a profile without it never enables the platform (S2)."""
    return bool((get_scoped_secret("ALICE_PATH_SECRET", "") or "").strip())


def register(ctx):
    STATE["home"] = Path(get_hermes_home())
    STATE["register_calls"] += 1
    _rec("register", n=STATE["register_calls"], loaded_at=LOADED_AT, home_ok=True)
    ctx.register_platform(
        name=PLATFORM,
        label="Yandex Alice (S5 probe)",
        adapter_factory=AliceS5,
        check_fn=lambda: True,
        validate_config=_validate,
        allowed_users_env="ALICE_OWNER_USER_ID",
        allowlist_suppresses_pairing=True,
        platform_hint="Voice channel probe: answer briefly.",
        display_tier="minimal",
    )
    ctx.register_platform(
        name=OTHER,
        label="S5 other platform",
        adapter_factory=OtherS5,
        check_fn=lambda: True,
        allowed_users_env="ALICE_OWNER_USER_ID",
        allowlist_suppresses_pairing=True,
        platform_hint="Probe channel: answer briefly.",
        display_tier="minimal",
    )
