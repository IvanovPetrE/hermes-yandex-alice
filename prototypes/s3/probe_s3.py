"""Prototype S3: detailed answers to Telegram through the core ``standalone_sender_fn``.

What it answers: can a ``kind: platform`` plugin living in the DEFAULT profile of a multiplexing
gateway deliver text to a configured Telegram chat with the default profile's bot through
``platform_registry.get("telegram").standalone_sender_fn(pconfig, chat_id, text)`` - from the
context of its own ``connect()`` and from a routed profile's scope, inside a running event loop,
with a result the plugin can tell apart and without leaking the token or the text into logs?

How: one process, one asyncio loop, NO GatewayRunner. A throwaway home with the default profile
(own bot token, chat setting), profile ``p2`` (no own token; ``display.platforms.telegram.notifications:
all`` and ``TELEGRAM_PROXY`` in its ``.env``) and profile ``p3`` (its own bot token). Profile scopes
are entered the way the gateway enters a routed turn (``set_hermes_home_override`` +
``set_secret_scope(build_profile_secret_scope(...))``, cf. ``gateway/run.py`` ``_profile_runtime_scope``).
Telegram is a fake Bot API on loopback (``extra.base_url``); a raw loopback recorder plays the proxy.
Tokens and ids are synthetic but token-shaped (Hermes redacts only ``<8+ digits>:<30+ chars>``).

Two layers, kept apart below:
- ``DetailsSender`` - the ADAPTER MODEL: imports only modules listed in ``public-api.md``
  (``gateway.config``, ``gateway.platforms._shared``, ``gateway.platform_registry``) plus stdlib;
- everything else - the HARNESS (homes, scopes, ``hermes_cli.plugins.discover_plugins``,
  ``hermes_logging``, the fake servers, peeks at private registry maps). PROBE ONLY: none of it goes
  into the plugin (``yandex-alice/``).

Run through ``run.sh`` only (throwaway HOME / HERMES_HOME / TMPDIR, see README.md). Python >= 3.11.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import gc
import json
import logging
import os
import socket
import sys
import threading
import time
import unittest
import warnings
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

# --- synthetic identities (token-shaped on purpose: see module docstring) -------------------------

DEFAULT_TOKEN = "1000000001:S3synthDEFAULTbotTokenBody_aaaaaaaaaaaaaaaa"
OWN_BOT_TOKEN = "1000000003:S3synthOWNBOTprofileTokenBody_bbbbbbbbbbbbb"
CHAT_ID = "100000042"
TEXT_MARK = "S3TEXTMARK"
TOKEN_BODIES = {DEFAULT_TOKEN.split(":", 1)[1]: "default", OWN_BOT_TOKEN.split(":", 1)[1]: "own_bot_profile"}


def _token_label(token: str) -> str:
    if token == DEFAULT_TOKEN:
        return "default"
    if token == OWN_BOT_TOKEN:
        return "own_bot_profile"
    return "empty" if not token else "other"


def _emit(kind: str, **fields: Any) -> None:
    print(json.dumps({"s3": kind, **fields}, ensure_ascii=False, default=str), flush=True)


def _rss_mb(field: str = "VmRSS:") -> int:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith(field):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def _fd_count() -> int:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return -1


def _bound_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    sock.setblocking(False)
    return sock


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --- harness: throwaway home (written before any Hermes module reads it) -------------------------


def _guard_temp_home() -> Path:
    raw = os.environ.get("HERMES_HOME") or ""
    home = Path(raw).resolve() if raw else None
    if home is None or not any(part.startswith("hya-s3-") for part in home.parts):
        raise SystemExit(f"refusing: HERMES_HOME={raw!r} is not inside a hya-s3-* temp directory")
    return home


ROOT = _guard_temp_home()
P2_HOME = ROOT / "profiles" / "p2"
P3_HOME = ROOT / "profiles" / "p3"
API_SOCK = _bound_socket()
PROXY_SOCK = _bound_socket()
API_BASE = f"http://127.0.0.1:{API_SOCK.getsockname()[1]}/bot"
PROXY_URL = f"http://127.0.0.1:{PROXY_SOCK.getsockname()[1]}"
CLOSED_BASE = f"http://127.0.0.1:{_closed_port()}/bot"
DEFAULT_ENV = {"TELEGRAM_BOT_TOKEN": DEFAULT_TOKEN, "ALICE_TELEGRAM_CHAT": CHAT_ID}


def _write_homes() -> None:
    for home in (ROOT, P2_HOME, P3_HOME):
        home.mkdir(parents=True, exist_ok=True)
    (ROOT / ".env").write_text("".join(f"{k}={v}\n" for k, v in DEFAULT_ENV.items()), encoding="utf-8")
    (ROOT / "config.yaml").write_text(
        "gateway:\n  multiplex_profiles: true\nlogging:\n  level: INFO\n", encoding="utf-8")
    (P2_HOME / ".env").write_text(f"TELEGRAM_PROXY={PROXY_URL}\n", encoding="utf-8")
    (P2_HOME / "config.yaml").write_text(
        "display:\n  platforms:\n    telegram:\n      notifications: all\n", encoding="utf-8")
    (P3_HOME / ".env").write_text(f"TELEGRAM_BOT_TOKEN={OWN_BOT_TOKEN}\n", encoding="utf-8")
    (P3_HOME / "config.yaml").write_text("display: {}\n", encoding="utf-8")
    # The gateway loads the default profile's .env into its process env at start.
    os.environ.update(DEFAULT_ENV)


_write_homes()


# --- ADAPTER MODEL (public API only: public-api.md) -----------------------------------------------

# Allow-list of plugin extra keys handed to the Telegram sender, prefixed so they never collide with
# the plugin's own keys (owner decision B3). Exact names are settled at stage 5/7.
TELEGRAM_EXTRA_KEYS = {"telegram_base_url": "base_url", "telegram_proxy_url": "proxy_url"}
DELIVERY_DEADLINE_S = 60.0  # owner decision B2: overall cap; past it the outcome is "uncertain"


class DetailsSender:
    """What the adapter would do: everything scope-dependent is read in ``connect()`` (the default
    profile's own scope); ``deliver()`` runs the send as a task in a copy of that context, with
    ``notify=True`` (owner decision B1), one overall deadline and no retries of its own."""

    def __init__(self, plugin_extra: Optional[dict] = None):
        self._plugin_extra = dict(plugin_extra or {})
        self.enabled = False
        self.disabled_reason: Optional[str] = "not_connected"
        self.pconfig = None
        self.chat_id: Optional[str] = None
        self.sender = None
        self.ctx: Optional[contextvars.Context] = None

    def connect(self) -> None:
        from gateway.config import PlatformConfig
        from gateway.platform_registry import platform_registry
        from gateway.platforms._shared import get_scoped_secret

        token = (get_scoped_secret("TELEGRAM_BOT_TOKEN", "") or "").strip()
        chat_id = (get_scoped_secret("ALICE_TELEGRAM_CHAT", "") or "").strip()
        entry = platform_registry.get("telegram")
        sender = getattr(entry, "standalone_sender_fn", None)
        if not token:
            self.disabled_reason = "no_token"  # never call the sender without a token (it would borrow)
        elif not chat_id:
            self.disabled_reason = "no_chat"
        elif sender is None:
            self.disabled_reason = "no_sender"
        else:
            self.disabled_reason = None
        self.enabled = self.disabled_reason is None
        if not self.enabled:
            self.pconfig = self.sender = None
            return
        extra = {dst: self._plugin_extra[src] for src, dst in TELEGRAM_EXTRA_KEYS.items() if self._plugin_extra.get(src)}
        self.pconfig = PlatformConfig(enabled=True, token=token, extra=extra)
        self.chat_id = chat_id
        self.sender = sender
        self.ctx = contextvars.copy_context()

    def deliver(self, text: str, *, deadline: float = DELIVERY_DEADLINE_S) -> "asyncio.Task[dict]":
        loop = asyncio.get_running_loop()
        if not self.enabled:
            return loop.create_task(self._disabled())
        return loop.create_task(self._deliver(text, deadline), context=self.ctx.copy())

    async def _disabled(self) -> dict:
        return {"status": "disabled", "reason": self.disabled_reason}

    async def _deliver(self, text: str, deadline: float) -> dict:
        try:
            result = await asyncio.wait_for(self.sender(self.pconfig, self.chat_id, text, notify=True), deadline)
        except asyncio.TimeoutError:
            return {"status": "uncertain", "reason": "deadline"}
        except Exception as exc:  # raised, not returned: report the class only, never retry
            return {"status": "uncertain", "reason": "raised", "exc": type(exc).__name__}
        if isinstance(result, dict) and result.get("success"):
            return {"status": "delivered", "message_id": result.get("message_id"), "raw": result}
        if isinstance(result, dict) and result.get("error"):
            return {"status": "failed", "error": result.get("error"), "raw": result}
        return {"status": "uncertain", "reason": "unexpected_result", "raw": result}


# --- harness: scopes ------------------------------------------------------------------------------


@contextlib.contextmanager
def profile_scope(home: Path):
    """A routed profile's scope, as the gateway enters it for a turn (home override + secret scope)."""
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home_token = set_hermes_home_override(str(home))
    secret_token = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
    try:
        yield
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


@contextlib.contextmanager
def default_bound_scope():
    """The default profile with its own secret scope bound (no home override)."""
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope

    token = set_secret_scope(build_profile_secret_scope(ROOT), profile_home=str(ROOT))
    try:
        yield
    finally:
        reset_secret_scope(token)


def _scope_state() -> dict:
    from agent.secret_scope import current_secret_scope, current_secret_scope_home
    from hermes_constants import get_hermes_home

    return {"home": str(get_hermes_home()), "secret_scope": current_secret_scope() is not None,
            "secret_scope_home": current_secret_scope_home()}


# --- harness: fake Bot API and proxy recorder -----------------------------------------------------


def _message_json(n: int, chat_id: Any) -> dict:
    try:
        cid = int(str(chat_id))
    except (TypeError, ValueError):
        cid = 0
    return {"ok": True, "result": {"message_id": n, "date": 1767225600, "chat": {"id": cid, "type": "private"},
                                   "text": "ok"}}


def _fail(code: int, description: str, **extra: Any) -> tuple[int, dict]:
    return code, {"ok": False, "error_code": code, "description": description, **extra}


class FakeBotApi:
    MODES = ("ok", "unauthorized", "forbidden", "chat_not_found", "parse", "server_error", "flood_then_ok",
             "html_502", "hang", "fail_second")

    def __init__(self) -> None:
        self.log: list[dict] = []
        self.mode = "ok"
        self._sends_in_mode = 0
        self._msg = 0
        self.release = asyncio.Event()
        self._runner = None
        self._proxy_server = None

    def reset(self, mode: str = "ok") -> None:
        assert mode in self.MODES, mode
        self.mode, self._sends_in_mode = mode, 0
        self.log.clear()

    def record(self, via: str, path: str, form: dict) -> dict:
        token, _, method = path[len("/bot"):].partition("/") if path.startswith("/bot") else ("?", "", path)
        entry = {"via": via, "method": method, "token": _token_label(token), "chat_id": form.get("chat_id"),
                 "text_len": len(form.get("text") or ""), "parse_mode": form.get("parse_mode"),
                 "disable_notification": form.get("disable_notification"), "mode": self.mode}
        self.log.append(entry)
        return entry

    def answer(self, method: str, form: dict) -> tuple[int, Any]:
        self._msg += 1
        ok = (200, _message_json(self._msg, form.get("chat_id")))
        if method != "sendMessage":
            return ok if method != "getMe" else (200, {"ok": True, "result": {"id": 1, "is_bot": True,
                                                                                "first_name": "s3", "username": "s3_bot"}})
        self._sends_in_mode += 1
        mode = self.mode
        if mode == "ok":
            return ok
        if mode == "unauthorized":
            return _fail(401, "Unauthorized")
        if mode == "forbidden":
            return _fail(403, "Forbidden: bot was blocked by the user")
        if mode == "chat_not_found":
            return _fail(400, "Bad Request: chat not found")
        if mode == "parse":
            return _fail(400, "Bad Request: can't parse entities: Can't find end of the entity starting at "
                              "byte offset 3") if form.get("parse_mode") else ok
        if mode == "server_error":
            return _fail(500, "Internal Server Error")
        if mode == "flood_then_ok":
            return _fail(429, "Too Many Requests: retry after 1", parameters={"retry_after": 1}) \
                if self._sends_in_mode == 1 else ok
        if mode == "html_502":
            return 502, "<html><body><h1>502 Bad Gateway</h1></body></html>"
        if mode == "fail_second":
            return _fail(400, "Bad Request: chat not found") if self._sends_in_mode == 2 else ok
        raise AssertionError(mode)

    async def _handler(self, request):
        from aiohttp import web

        form: dict = {}
        if request.can_read_body:
            if "json" in (request.content_type or ""):
                form = dict(await request.json())
            else:
                form = {k: v for k, v in (await request.post()).items() if isinstance(v, str)}
        entry = self.record("api", request.path, form)
        if self.mode == "hang" and entry["method"] == "sendMessage":
            await self.release.wait()
        code, body = self.answer(entry["method"], form)
        if isinstance(body, str):
            return web.Response(status=code, text=body, content_type="text/html")
        return web.json_response(body, status=code)

    async def _proxy_conn(self, reader, writer) -> None:
        """A forward proxy that answers itself: records the absolute-form request line and replies ok."""
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
            headers = {k.lower(): v for k, _, v in (line.partition(": ") for line in head[1:] if line)}
            size = int(headers.get("content-length") or 0)
            raw = (await reader.readexactly(size)).decode("utf-8", "replace") if size else ""
            form = {k: v[0] for k, v in parse_qs(raw).items()} if "json" not in headers.get("content-type", "") \
                else dict(json.loads(raw or "{}"))
            parts = head[0].split(" ")
            path = urlsplit(parts[1]).path if len(parts) > 1 else "?"
            entry = self.record("proxy", path, form)
            _code, body = (200, _message_json(10_000 + len(self.log), form.get("chat_id")))
            if entry["method"] != "sendMessage":
                _code, body = self.answer(entry["method"], form)
            data = json.dumps(body).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                         + f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
            await writer.drain()
        except Exception as exc:  # noqa: BLE001 - harness: report and drop the connection
            _emit("proxy_error", exc=type(exc).__name__)
        finally:
            writer.close()

    async def start(self) -> None:
        from aiohttp import web

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handler)
        self._runner = web.AppRunner(app, access_log=None)  # no access log: paths carry tokens
        await self._runner.setup()
        await web.SockSite(self._runner, API_SOCK).start()
        self._proxy_server = await asyncio.start_server(self._proxy_conn, sock=PROXY_SOCK)

    async def stop(self) -> None:
        self.release.set()
        if self._proxy_server is not None:
            self._proxy_server.close()
        if self._runner is not None:
            await self._runner.cleanup()


class Ticker:
    """Event-loop lag meter: the largest overshoot of a 10 ms sleep."""

    def __init__(self) -> None:
        self.max_lag_ms = 0.0
        self._stop = False
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        while not self._stop:
            t0 = time.monotonic()
            await asyncio.sleep(0.01)
            self.max_lag_ms = max(self.max_lag_ms, (time.monotonic() - t0 - 0.01) * 1000)

    async def stop(self) -> float:
        self._stop = True
        if self._task is not None:
            await self._task
        return round(self.max_lag_ms, 1)


# --- harness: module state ------------------------------------------------------------------------

LOOP = asyncio.new_event_loop()
API: FakeBotApi
MODEL = DetailsSender({"telegram_base_url": API_BASE})
RSS_PEAK = {"mb": 0}


def run(coro, timeout: float = 120.0):
    return LOOP.run_until_complete(asyncio.wait_for(coro, timeout))


def _start_rss_watchdog() -> None:
    limit = int(os.environ.get("S3_RSS_LIMIT_MB") or 0)

    def watch() -> None:
        while True:
            rss = _rss_mb()
            RSS_PEAK["mb"] = max(RSS_PEAK["mb"], rss)
            if limit and rss > limit:
                print(f"\n!!! RSS {rss} MB > limit {limit} MB - probe aborted", flush=True)
                os._exit(137)
            time.sleep(0.5)

    threading.Thread(target=watch, name="s3-rss-watchdog", daemon=True).start()


def _assert_loopback(pconfig) -> None:
    base = (getattr(pconfig, "extra", None) or {}).get("base_url") or ""
    if not base.startswith("http://127.0.0.1:") or not base.endswith("/bot"):
        raise SystemExit(f"refusing: base_url {base!r} is not the loopback fake Bot API")


def _leaks(value: Any) -> list[str]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    found = [f"token:{label}" for body, label in TOKEN_BODIES.items() if body in text]
    return found + (["text"] if TEXT_MARK in text else [])


def setUpModule() -> None:  # noqa: N802 - unittest hook
    global API
    _start_rss_watchdog()
    _emit("start", rss_mb=_rss_mb(), fds=_fd_count(), python=sys.version.split()[0])

    import hermes_logging
    from agent.secret_scope import set_multiplex_active

    hermes_logging.setup_logging(hermes_home=ROOT, mode="gateway")  # gateway-style files, levels, redaction
    for name in ("aiohttp", "aiohttp.access", "aiohttp.server", "aiohttp.web"):
        logging.getLogger(name).setLevel(logging.CRITICAL)  # harness servers only
    set_multiplex_active(True)

    from hermes_cli.plugins import discover_plugins

    timings = {}
    for label, scope in (("default", contextlib.nullcontext()), ("p2", profile_scope(P2_HOME)),
                         ("p3", profile_scope(P3_HOME))):
        with scope:
            t0 = time.monotonic()
            discover_plugins()
            timings[label] = round((time.monotonic() - t0) * 1000)
    _emit("discover_plugins_ms", **timings, rss_mb=_rss_mb())

    API = FakeBotApi()
    LOOP.run_until_complete(API.start())


def tearDownModule() -> None:  # noqa: N802 - unittest hook
    LOOP.run_until_complete(API.stop())
    LOOP.close()
    _emit("end", rss_mb=_rss_mb(), rss_peak_mb=max(RSS_PEAK["mb"], _rss_mb("VmHWM:")), fds=_fd_count())


async def _deliver_case(name: str, mode: str, model: DetailsSender = MODEL, *, text: Optional[str] = None,
                        deadline: float = DELIVERY_DEADLINE_S) -> dict:
    API.reset(mode)
    if model.pconfig is not None:
        _assert_loopback(model.pconfig)
    body = text or f"{TEXT_MARK} {name}: подробный ответ."
    t0 = time.monotonic()
    outcome = await model.deliver(body, deadline=deadline)
    elapsed = round(time.monotonic() - t0, 2)
    sends = [e for e in API.log if e["method"] == "sendMessage"]
    record = {"case": name, "mode": mode, "status": outcome.get("status"), "elapsed_s": elapsed,
              "requests": len(API.log), "send_requests": len(sends),
              "methods": sorted({e["method"] for e in API.log}),
              "tokens": sorted({e["token"] for e in API.log}),
              "outcome": {k: v for k, v in outcome.items() if k != "raw"}, "raw": outcome.get("raw"),
              "sends": sends, "leaks_in_result": _leaks(outcome)}
    _emit("deliver", **record)
    return record


def _ensure_connected() -> None:
    if not MODEL.enabled:
        MODEL.connect()
        assert MODEL.enabled, MODEL.disabled_reason


# --- scenarios ------------------------------------------------------------------------------------


class A_Registry(unittest.TestCase):
    """Run first: the first ``platform_registry.get("telegram")`` in each scope, in a fresh process."""

    def test_1_first_get_and_connect(self):
        from gateway.platform_registry import platform_registry

        async def scenario():
            ticker = Ticker()
            ticker.start()
            await asyncio.sleep(0.05)
            t0 = time.monotonic()
            MODEL.connect()  # sync inside the loop, as the adapter's async connect() would run it
            connect_ms = round((time.monotonic() - t0) * 1000, 1)
            await asyncio.sleep(0.05)
            out = {"connect_ms": connect_ms, "connect_lag_ms": await ticker.stop(), "connect_scope": _scope_state(),
                   "enabled": MODEL.enabled, "disabled_reason": MODEL.disabled_reason,
                   "pconfig_token": _token_label(getattr(MODEL.pconfig, "token", "") or ""),
                   "pconfig_extra_keys": sorted((getattr(MODEL.pconfig, "extra", None) or {}).keys())}
            for label, home in (("p2", P2_HOME), ("p3", P3_HOME)):
                with profile_scope(home):
                    ticker = Ticker()
                    ticker.start()
                    await asyncio.sleep(0.05)
                    t0 = time.monotonic()
                    entry = platform_registry.get("telegram")
                    out[f"get_{label}_ms"] = round((time.monotonic() - t0) * 1000, 1)
                    await asyncio.sleep(0.05)
                    out[f"get_{label}_lag_ms"] = await ticker.stop()
                    out[f"sender_{label}"] = getattr(entry, "standalone_sender_fn", None) is not None
                    out[f"same_sender_{label}"] = getattr(entry, "standalone_sender_fn", None) is MODEL.sender
            return out

        out = run(scenario())
        # harness peek (private maps): where the telegram entry lives
        reg = platform_registry
        out["registry_global_entry"] = "telegram" in getattr(reg, "_entries", {})
        out["registry_global_deferred"] = "telegram" in getattr(reg, "_deferred", {})
        out["registry_scoped_entry_scopes"] = sum("telegram" in m for m in getattr(reg, "_scoped_entries", {}).values())
        _emit("registry", **out, rss_mb=_rss_mb())
        self.assertTrue(MODEL.enabled, MODEL.disabled_reason)
        self.assertEqual(out["pconfig_token"], "default")
        self.assertTrue(out["sender_p2"] and out["sender_p3"])


class B_TokenScope(unittest.TestCase):
    def setUp(self):
        _ensure_connected()
        API.reset("ok")

    def _bare_pconfig(self, token):
        from gateway.config import PlatformConfig

        pconfig = PlatformConfig(enabled=True, token=token, extra={"base_url": API_BASE})
        _assert_loopback(pconfig)
        return pconfig

    async def _send(self, pconfig, text, **kw):
        try:
            result = await MODEL.sender(pconfig, CHAT_ID, text, **kw)
            return {"returned": {k: v for k, v in result.items()}}
        except Exception as exc:  # noqa: BLE001 - observed, not handled
            return {"raised": type(exc).__name__}

    def test_1_default_token_none_raises(self):
        async def scenario():
            return await self._send(self._bare_pconfig(None), f"{TEXT_MARK} T1"), _scope_state()

        res, scope = run(scenario())
        _emit("T1", **res, scope=scope, requests=list(API.log))
        self.assertEqual(res.get("raised"), "UnscopedSecretError")
        self.assertEqual(API.log, [])

    def test_2a_connect_unscoped(self):
        async def scenario():
            model = DetailsSender({"telegram_base_url": API_BASE})
            model.connect()
            return model, _scope_state(), await _deliver_case("T2a", "ok", model)

        model, scope, rec = run(scenario())
        _emit("T2a", scope=scope, pconfig_token=_token_label(model.pconfig.token))
        self.assertEqual(rec["tokens"], ["default"])
        self.assertEqual(rec["status"], "delivered")

    def test_2b_connect_default_bound(self):
        async def scenario():
            with default_bound_scope():
                model = DetailsSender({"telegram_base_url": API_BASE})
                model.connect()
                scope = _scope_state()
            return model, scope, await _deliver_case("T2b", "ok", model)

        model, scope, rec = run(scenario())
        _emit("T2b", scope=scope, pconfig_token=_token_label(model.pconfig.token))
        self.assertTrue(scope["secret_scope"])
        self.assertEqual(rec["tokens"], ["default"])

    def test_3_no_own_token_profile_token_none(self):
        async def scenario():
            with profile_scope(P2_HOME):
                return await self._send(self._bare_pconfig(None), f"{TEXT_MARK} T3"), _scope_state()

        res, scope = run(scenario())
        _emit("T3", **res, scope=scope, requests=list(API.log), leaks=_leaks(res))
        self.assertIn("error", res.get("returned", {}))
        self.assertEqual(API.log, [])

    def test_4_own_bot_profile_token_none(self):
        async def scenario():
            with profile_scope(P3_HOME):
                return await self._send(self._bare_pconfig(None), f"{TEXT_MARK} T4"), _scope_state()

        res, scope = run(scenario())
        _emit("T4", **res, scope=scope, requests=list(API.log))
        self.assertEqual([e["token"] for e in API.log], ["own_bot_profile"])

    def test_5_vs_6_call_scope_vs_connect_context(self):
        """T5: model pconfig, called right in p2's scope. T6: same call as a task in connect()'s context.
        ``notify=None`` on purpose: ``notify=True`` would hide the notification mode of the scope."""

        async def scenario():
            with profile_scope(P2_HOME):
                t5 = await self._send(MODEL.pconfig, f"{TEXT_MARK} T5")
                t5_log = list(API.log)
                API.reset("ok")
                task = asyncio.get_running_loop().create_task(
                    self._send(MODEL.pconfig, f"{TEXT_MARK} T6"), context=MODEL.ctx.copy())
                t6 = await task
                t6_log = list(API.log)
                API.reset("ok")
                t6n = await MODEL.deliver(f"{TEXT_MARK} T6 notify")  # the model itself: notify=True
                t6n_log = list(API.log)
                caller = _scope_state()
            from hermes_constants import get_hermes_home
            ctx_home = MODEL.ctx.copy().run(get_hermes_home)
            return t5, t5_log, t6, t6_log, t6n, t6n_log, caller, str(ctx_home)

        t5, t5_log, t6, t6_log, t6n, t6n_log, caller, ctx_home = run(scenario())
        _emit("T5", **t5, requests=t5_log, caller_scope=caller)
        _emit("T6", **t6, requests=t6_log, connect_ctx_home=ctx_home)
        _emit("T6notify", status=t6n.get("status"), requests=t6n_log)
        sig = lambda log: [(e["via"], e["token"], e["disable_notification"]) for e in log]  # noqa: E731
        self.assertEqual({e["token"] for e in t5_log + t6_log}, {"default"})
        self.assertNotEqual(sig(t5_log), sig(t6_log), "T5 and T6 indistinguishable: connect() context not shown")
        self.assertEqual(Path(ctx_home).resolve(), ROOT)
        self.assertEqual([e["via"] for e in t6n_log], ["api"])


class C_Results(unittest.TestCase):
    def setUp(self):
        _ensure_connected()

    def test_01_ok(self):
        rec = run(_deliver_case("ok", "ok"))
        self.assertEqual(rec["status"], "delivered")
        self.assertEqual(rec["send_requests"], 1)
        self.assertIsNone(rec["sends"][0]["disable_notification"])  # notify=True rings
        self.assertEqual(rec["leaks_in_result"], [])

    def test_02_no_token(self):
        saved = os.environ.pop("TELEGRAM_BOT_TOKEN")
        try:
            async def scenario():
                model = DetailsSender({"telegram_base_url": API_BASE})
                model.connect()
                rec = await _deliver_case("no_token_connect_ctx", "ok", model)
                # for the record: what the sender itself does with an empty pconfig token
                from gateway.config import PlatformConfig
                bare = PlatformConfig(enabled=True, token="", extra={"base_url": API_BASE})
                API.reset("ok")
                try:
                    unscoped = {"returned": await MODEL.sender(bare, CHAT_ID, f"{TEXT_MARK} empty default")}
                except Exception as exc:  # noqa: BLE001
                    unscoped = {"raised": type(exc).__name__}
                unscoped_log = list(API.log)
                API.reset("ok")
                with profile_scope(P3_HOME):
                    p3_model = DetailsSender({"telegram_base_url": API_BASE})
                    p3_model.connect()  # connect() run in a profile with its own bot: shows why it must not
                    try:
                        in_p3 = {"returned": await MODEL.sender(bare, CHAT_ID, f"{TEXT_MARK} empty p3")}
                    except Exception as exc:  # noqa: BLE001
                        in_p3 = {"raised": type(exc).__name__}
                return model, rec, unscoped, unscoped_log, in_p3, list(API.log), p3_model

            model, rec, unscoped, unscoped_log, in_p3, p3_log, p3_model = run(scenario())
        finally:
            os.environ["TELEGRAM_BOT_TOKEN"] = saved
        _emit("no_token", model_disabled_reason=model.disabled_reason,
              empty_token_unscoped=unscoped, empty_token_unscoped_requests=unscoped_log,
              empty_token_in_own_bot_profile=in_p3, empty_token_in_own_bot_profile_requests=p3_log,
              connect_in_own_bot_profile_token=_token_label(getattr(p3_model.pconfig, "token", "") or ""))
        self.assertEqual(rec["status"], "disabled")
        self.assertEqual(rec["requests"], 0)

    def _refused(self, name, mode):
        rec = run(_deliver_case(name, mode))
        self.assertEqual(rec["status"], "failed")
        self.assertEqual(rec["send_requests"], 1)
        self.assertEqual(rec["leaks_in_result"], [])
        return rec

    def test_03_unauthorized(self):
        self._refused("401", "unauthorized")

    def test_04_forbidden(self):
        self._refused("403", "forbidden")

    def test_05_chat_not_found(self):
        self._refused("400_chat", "chat_not_found")

    def test_06_server_error(self):
        rec = run(_deliver_case("500_json", "server_error"))
        self.assertEqual(rec["status"], "failed")
        self.assertEqual(rec["leaks_in_result"], [])

    def test_07_flood_then_ok(self):
        rec = run(_deliver_case("429_then_ok", "flood_then_ok"))
        self.assertEqual(rec["status"], "delivered")

    def test_08_html_502(self):
        rec = run(_deliver_case("html_502", "html_502"))
        self.assertNotEqual(rec["status"], "delivered")

    def test_09_closed_port(self):
        async def scenario():
            model = DetailsSender({"telegram_base_url": CLOSED_BASE})
            model.connect()
            return await _deliver_case("closed_port", "ok", model)

        rec = run(scenario())
        self.assertEqual(rec["status"], "failed")
        self.assertEqual(rec["requests"], 0)

    def test_10_hang_natural_timeout(self):
        rec = run(_deliver_case("hang_natural", "hang"), timeout=180)
        API.release.set()
        API.release = asyncio.Event()
        self.assertEqual(rec["send_requests"], 1)
        self.assertNotEqual(rec["status"], "delivered")

    def test_11_parse_error_plain_fallback(self):
        rec = run(_deliver_case("parse_fallback", "parse", text=f"{TEXT_MARK} *жирный* и _курсив_ [ссылка](http://example.org)"))
        self.assertEqual(rec["status"], "delivered")

    def test_12_long_text(self):
        text = f"{TEXT_MARK} длинный: " + "Подробный ответ, абзац за абзацем. " * 260
        rec = run(_deliver_case("long_ok", "ok", text=text))
        self.assertEqual(rec["status"], "delivered")
        self.assertGreater(rec["send_requests"], 1)
        rec2 = run(_deliver_case("long_fail_second", "fail_second", text=text))
        _emit("long_fail_second_summary", status=rec2["status"], send_requests=rec2["send_requests"],
              text_len=len(text))
        self.assertEqual(rec2["status"], "failed")

    def test_13_deadline(self):
        rec = run(_deliver_case("deadline_2s", "hang", deadline=2.0))
        API.release.set()
        API.release = asyncio.Event()
        self.assertEqual(rec["status"], "uncertain")
        self.assertEqual(rec["send_requests"], 1)
        self.assertLess(rec["elapsed_s"], 4.0)


class D_Loop(unittest.TestCase):
    def setUp(self):
        _ensure_connected()

    def test_1_hang_does_not_block_loop(self):
        from aiohttp import ClientSession, web

        async def scenario():
            app = web.Application()
            app.router.add_get("/ping", lambda _r: web.Response(text="pong"))
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            sock = _bound_socket()
            await web.SockSite(runner, sock).start()
            url = f"http://127.0.0.1:{sock.getsockname()[1]}/ping"
            ticker = Ticker()
            ticker.start()
            API.reset("hang")
            task = MODEL.deliver(f"{TEXT_MARK} loop", deadline=5.0)
            await asyncio.sleep(0.5)
            pings = []
            async with ClientSession() as session:
                for _ in range(5):
                    t0 = time.monotonic()
                    async with session.get(url) as resp:
                        await resp.text()
                    pings.append(round((time.monotonic() - t0) * 1000, 1))
                    await asyncio.sleep(0.2)
            outcome = await task
            lag = await ticker.stop()
            await runner.cleanup()
            return pings, lag, outcome, list(API.log)

        pings, lag, outcome, log = run(scenario())
        API.release.set()
        API.release = asyncio.Event()
        _emit("loop_hang", pings_ms=pings, max_lag_ms=lag, outcome=outcome, requests=log)
        self.assertLess(max(pings), 100.0)
        self.assertLess(lag, 50.0)
        self.assertEqual(outcome["status"], "uncertain")

    def test_2_fd_and_resource_warnings(self):
        async def scenario():
            API.reset("ok")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                gc.collect()
                before = _fd_count()
                statuses = []
                for i in range(20):
                    statuses.append((await MODEL.deliver(f"{TEXT_MARK} fd {i}"))["status"])
                await asyncio.sleep(0.3)
                after_sends = _fd_count()
                gc.collect()
                await asyncio.sleep(0.3)
                after_gc = _fd_count()
            kinds: dict[str, int] = {}
            for w in caught:
                kinds[w.category.__name__] = kinds.get(w.category.__name__, 0) + 1
            return before, after_sends, after_gc, statuses, kinds

        before, after_sends, after_gc, statuses, kinds = run(scenario())
        _emit("fds", before=before, after_20_sends=after_sends, after_gc=after_gc,
              delivered=statuses.count("delivered"), warnings=kinds, rss_mb=_rss_mb())
        self.assertEqual(statuses.count("delivered"), 20)


class E_Logs(unittest.TestCase):
    """Run last. (a) gateway-style files decide; (b) raw DEBUG of every logger is for the record only."""

    def _files(self) -> dict:
        import hermes_logging

        hermes_logging.flush_log_queue()
        hermes_logging.drain_log_queue(2.0)
        out = {}
        for path in sorted((ROOT / "logs").glob("*.log")):
            out[path.name] = path.read_text(encoding="utf-8", errors="replace")
        return out

    def test_1_gateway_style_logs(self):
        files = self._files()
        summary = {name: {"lines": text.count("\n"), "leaks": _leaks(text)} for name, text in files.items()}
        warn_lines = [line for text in files.values() for line in text.splitlines()
                      if " WARNING " in line or " ERROR " in line]
        _emit("logs_gateway_style", files=summary, warning_lines=len(warn_lines), sample=warn_lines[:12])
        self.assertTrue(files, "no gateway-style log files written")
        for name, info in summary.items():
            self.assertEqual(info["leaks"], [], f"leak in {name}")

    def test_2_raw_debug_for_the_record(self):
        records: list[str] = []

        class Memory(logging.Handler):
            def emit(self, record):
                try:
                    records.append(f"{record.name} {record.levelname} {record.getMessage()}")
                except Exception:  # noqa: BLE001
                    records.append(f"{record.name} {record.levelname} <unformattable>")

        names = ("", "httpx", "httpcore", "telegram", "telegram.ext", "plugins", "gateway")
        saved = {n: logging.getLogger(n).level for n in names}
        handler = Memory(level=logging.DEBUG)
        logging.getLogger().addHandler(handler)
        try:
            for n in names:
                logging.getLogger(n).setLevel(logging.DEBUG)
            ok = run(_deliver_case("raw_debug_ok", "ok"))
            bad = run(_deliver_case("raw_debug_401", "unauthorized"))
        finally:
            logging.getLogger().removeHandler(handler)
            for n, level in saved.items():
                logging.getLogger(n).setLevel(level)
        raw_leaks: dict[str, int] = {}
        for line in records:
            for leak in _leaks(line):
                key = f"{line.split(' ', 1)[0]}:{leak}"
                raw_leaks[key] = raw_leaks.get(key, 0) + 1
        files = self._files()
        _emit("logs_raw_debug", records=len(records), leaks_by_logger=raw_leaks,
              files_after_debug={name: _leaks(text) for name, text in files.items()},
              statuses=[ok["status"], bad["status"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
