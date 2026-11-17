"""S7 probe plugin: reading a voice conversation's last answer back from Hermes' history.

The model of the stage-3 adapter. Imports only modules listed in Hermes' ``public-api.md`` (``gateway.config``,
``gateway.platforms.base`` / ``event`` / ``_shared``, ``hermes_constants``, and, inside ``history.py``,
``hermes_state``). It never reads ``os.environ``. There is no HTTP listener (S5 proved it): the harness injects
turns and asks for recalls through the child process.

* ``send()`` / hooks journal metadata like S5/S6 (the final rule, ``recovery.classify_send``).
* ``probe_recall`` runs ``history.read_last_answer`` in a worker thread under a deadline (``wait_for``) - the
  stage-3 shape of ``RecallLast`` -> ``Recalled``.
* ``hold_final`` (a file in ``s7-control``) parks the final ``send()`` so the harness can SIGKILL the gateway
  inside it (S5, R3).

``JOURNAL`` lines go to ``<home>/s7-journal.jsonl`` for the harness: ids, flags, lengths and the first
characters of SYNTHETIC scripted text; never a secret.
"""

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path

from gateway.config import Platform
from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from hermes_constants import get_hermes_home

from . import history, recovery

PLATFORM = "yandex_alice"
OWNER_ID = "u-probe-owner"
STATE = {"home": None}
logger = logging.getLogger(__name__)


def _rec(op, **kw):
    home = STATE["home"]
    if home is None:
        return
    row = dict(t=round(time.monotonic(), 3), pid=os.getpid(), op=op, **kw)
    try:
        with (home / "s7-journal.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


class AliceS7(BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self, config):
        super().__init__(config, Platform(PLATFORM))
        extra = dict(getattr(config, "extra", None) or {})
        self.interactive_resume = bool(extra.get("interactive_resume", False))
        self._open = {}  # chat -> message id of the open turn
        self._n = 0
        _rec("create", adapter_id=id(self))

    async def connect(self, *, is_reconnect=False):
        self._running = True
        self._mark_connected()
        _rec("connect", is_reconnect=is_reconnect)
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
        kind = recovery.classify_send(
            open_turn=chat in self._open, turn_mid=self._open.get(chat), reply_to=reply,
            notify=meta.get("notify"), interim=bool(meta.get("_interim_send")), text=text)
        _rec("send", chat=chat, kind=kind, open_turn=chat in self._open, reply_to=reply, notify=meta.get("notify"),
             interim=bool(meta.get("_interim_send")), length=len(text), head=text[:80],
             marker=bool(recovery.split_marker(text)[0]))
        control = STATE["home"] / "s7-control"
        if kind == recovery.FINAL and (control / "hold_final").exists():
            _rec("held", chat=chat)  # the harness kills the gateway here: a crash inside the final send
            await asyncio.sleep(3600)
        return SendResult(success=True, message_id="out-%d" % self._n)

    async def on_processing_start(self, event):
        chat = str(event.source.chat_id)
        self._open[chat] = event.message_id
        _rec("start", chat=chat, mid=event.message_id, internal=bool(getattr(event, "internal", False)),
             text_len=len(event.text or ""), profile=getattr(event.source, "profile", None),
             scope_home=str(get_hermes_home()))

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

    # -- harness entry points --

    async def probe_inject(self, text, message_id, chat_id):
        source = self.build_source(chat_id=chat_id, chat_name="chat-%s" % chat_id, chat_type="dm",
                                   user_id=OWNER_ID, user_name="probe")
        await self.handle_message(MessageEvent(text=text, message_type=MessageType.TEXT,
                                               source=source, message_id=message_id))

    async def probe_recall(self, chat_id, deadline_s=1.5, **kw):
        """``RecallLast`` -> ``Recalled``: the history read in a worker thread under a deadline."""
        t0 = time.perf_counter()
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(history.read_last_answer, PLATFORM, str(chat_id), **kw), deadline_s)
        except asyncio.TimeoutError:
            res = dict(outcome=history.ERROR, text=None, why="deadline", steps={}, parts=0)
        except Exception as exc:  # noqa: BLE001
            res = dict(outcome=history.ERROR, text=None, why=type(exc).__name__, steps={}, parts=0)
        res["took_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        res["scope_home"] = str(get_hermes_home())
        _rec("recall", chat=str(chat_id), outcome=res["outcome"], why=res.get("why"), took_ms=res["took_ms"],
             head=(res.get("text") or "")[:60])
        return res


def process_stats():
    """Threads and open file descriptors of this process (the harness compares them before/after storms)."""
    try:
        fds = len(os.listdir("/proc/self/fd"))
    except OSError:
        fds = -1
    return dict(threads=threading.active_count(), fds=fds)


def _validate(config):
    """The secret must exist in the CURRENT scope: a profile without it never enables the platform (S2)."""
    return bool((get_scoped_secret("ALICE_PATH_SECRET", "") or "").strip())


def register(ctx):
    STATE["home"] = Path(get_hermes_home())
    _rec("register")
    ctx.register_platform(
        name=PLATFORM,
        label="Yandex Alice (S7 probe)",
        adapter_factory=AliceS7,
        check_fn=lambda: True,
        validate_config=_validate,
        allowed_users_env="ALICE_OWNER_USER_ID",
        allowlist_suppresses_pairing=True,
        platform_hint="Voice channel probe: answer briefly.",
        display_tier="minimal",
    )
