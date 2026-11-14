"""Prototype S2: which profile runs a plugin platform under the multiplexing gateway.

What it answers: with ``gateway.multiplex_profiles`` on, can a ``kind: platform`` plugin (the
future ``yandex_alice`` adapter) live in the DEFAULT profile - plugin directory, ``plugins.enabled``,
``platforms.yandex_alice``, ``ALICE_*`` in its ``.env`` - while a platform-wide
``gateway.profile_routes`` entry runs every turn in another profile (``p2``): p2's SOUL, p2's
``platform_toolsets.yandex_alice``, p2's ``state.db`` and history; one adapter, one listener; the
owner allowlist read from the default profile. And does the plugin's ``platform_hint`` /
``display_tier`` reach a turn that runs in p2 (plugin registrations are scoped per HERMES_HOME)?

How: a REAL ``GatewayRunner`` with a throwaway default home and profile ``p2``. The stub is a REAL
plugin directory (``plugin.yaml`` + ``__init__.py`` with ``register(ctx)`` ->
``ctx.register_platform``) found by Hermes' own plugin discovery; the stub imports only modules
listed in ``public-api.md``. Its ``connect()`` binds one real loopback socket. The agent is Hermes'
scripted loopback LLM (``tests/fakes/fake_llm_provider``): no real model, keys or cost. One process
per group (see ``GROUPS``).

PROBE ONLY. ``gateway.run``, ``tests.*`` and the runner's private attributes are NOT the public
plugin API - they are the harness that starts and inspects a gateway. The plugin itself
(``yandex-alice/``) must never use them.

Run through ``run.sh`` only (throwaway HOME / HERMES_HOME / lock dir, see README.md).
Python >= 3.11.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Optional

PLATFORM = "yandex_alice"
PLUGIN_NAME = "yandex-alice-probe"
P2 = "p2"
OWNER_ID = "u-probe-owner"  # synthetic; written to the DEFAULT profile's .env only
STRANGER_ID = "u-probe-stranger"
OWNER_ENV = "ALICE_OWNER_USER_ID"
SECRET_ENV = "ALICE_PROBE_SECRET"
SOUL_DEFAULT = "SOUL-DEFAULT-MARK"
SOUL_P2 = "SOUL-P2-MARK"
HINT_PLUGIN = "HINT-PLUGIN-MARK"
HINT_CONFIG = "HINT-CONFIG-MARK"
TOOL_P2 = "todo_list"  # toolset "todo" -> p2's platform_toolsets.yandex_alice
TOOL_DEFAULT = "cronjob_manage"  # toolset "cronjob" -> default's platform_toolsets.yandex_alice
TURN_TIMEOUT = 120.0
SETTLE = 1.5
FOREIGN_WAIT = 8.0
# Tool Search (default "auto") replaces deferred tools - todo_list and cronjob_manage among them -
# with tool_search/tool_call bridges, so the request would not show which toolsets the turn has.
TOOLS_EAGER = "tools:\n  tool_search:\n    enabled: 'off'\n"

# plugin_in_p2: the plugin directory is also in profiles/p2/plugins and enabled in p2's config.
# enable_in_p2: p2's config also enables platforms.yandex_alice (a second adapter would be built).
# p2_overrides: p2's config carries platform_hints.yandex_alice + display.platforms.yandex_alice.
# suppress: allowlist_suppresses_pairing passed to register_platform.
# gate: register validate_config requiring ALICE_PATH_SECRET in the CURRENT scope (as the real
#   adapter will); without it is_connected falls back to check_fn (always True here).
# dm_ignore: the default profile's platforms.yandex_alice.extra.unauthorized_dm_behavior: ignore.
_BASE = dict(plugin_in_p2=False, enable_in_p2=False, p2_overrides=False, suppress=True,
             gate=False, dm_ignore=False)
GROUPS: dict[str, dict[str, Any]] = {
    "A1": dict(_BASE),
    "A2": dict(_BASE, plugin_in_p2=True),
    "A2g": dict(_BASE, plugin_in_p2=True, gate=True),
    "A3": dict(_BASE, p2_overrides=True),
    "A4": dict(_BASE, dm_ignore=True),
    "A0": dict(_BASE, suppress=False),
    # The misconfiguration to document: the platform enabled AND configured in p2 as well.
    "B": dict(_BASE, plugin_in_p2=True, enable_in_p2=True, gate=True, p2_path_secret=True),
}
PATH_SECRET_ENV = "ALICE_PATH_SECRET"  # synthetic value, default profile's .env only

PLUGIN_YAML = f"""\
name: {PLUGIN_NAME}
label: Yandex Alice (S2 probe)
kind: platform
version: 0.0.1
description: S2 probe stub, no network except one loopback listener
"""

# The stub plugin. Imports: gateway.config, gateway.platforms._shared, gateway.platforms.base,
# gateway.platforms.event, hermes_constants - all listed in public-api.md.
PLUGIN_INIT = '''\
"""S2 probe stub platform: journals every gateway call, binds one loopback socket."""
import asyncio
import time

from gateway.config import Platform
from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from hermes_constants import get_hermes_home

PLATFORM = "yandex_alice"


def _secret():
    try:
        return get_scoped_secret("ALICE_PROBE_SECRET")
    except Exception as exc:  # recorded, not raised: the probe wants to see it
        return "!" + type(exc).__name__


class ProbeAdapter(BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self, config):
        super().__init__(config, Platform(PLATFORM))
        self.probe_ops = []
        self._probe_server = None
        self._n = 0
        self._rec("init")

    def _rec(self, op, **kw):
        self.probe_ops.append(dict(t=time.monotonic(), op=op, home=str(get_hermes_home()),
                                   secret=_secret(), **kw))

    async def connect(self, *, is_reconnect=False):
        port = int((getattr(self.config, "extra", None) or {}).get("port") or 0)
        try:
            self._probe_server = await asyncio.start_server(self._on_client, "127.0.0.1", port)
            bound = "ok"
        except OSError as exc:
            bound = "OSError:%s" % exc.errno
        self._rec("connect", bound=bound, port=port, reconnect=bool(is_reconnect))
        if bound != "ok":
            return False
        self._running = True
        self._mark_connected()
        return True

    async def _on_client(self, reader, writer):
        writer.close()

    async def disconnect(self):
        if self._probe_server is not None:
            self._probe_server.close()
            await self._probe_server.wait_closed()
            self._probe_server = None
        self._running = False
        self._mark_disconnected()
        self._rec("disconnect")

    async def get_chat_info(self, chat_id):
        return {"name": "chat-%s" % chat_id, "type": "dm", "chat_id": chat_id}

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self._n += 1
        meta = dict(metadata or {})
        self._rec("send", chat=str(chat_id), content=content or "",
                  reply_to=None if reply_to is None else str(reply_to),
                  notify=meta.get("notify"), interim=bool(meta.get("_interim_send")))
        return SendResult(success=True, message_id="out-%d" % self._n)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        self._rec("edit", chat=str(chat_id), content=content or "", reply_to=str(message_id))
        return SendResult(success=False, error="Not supported")

    async def delete_message(self, chat_id, message_id):
        self._rec("delete", chat=str(chat_id), reply_to=str(message_id))
        return False

    async def on_processing_start(self, event):
        self._rec("start", chat=str(event.source.chat_id), mid=event.message_id,
                  profile=getattr(event.source, "profile", None))

    async def on_processing_complete(self, event, outcome):
        self._rec("complete", chat=str(event.source.chat_id), mid=event.message_id,
                  profile=getattr(event.source, "profile", None),
                  outcome=getattr(outcome, "value", str(outcome)))

    async def probe_inject(self, text, message_id, chat_id, user_id):
        source = self.build_source(chat_id=chat_id, chat_name="chat-%s" % chat_id, chat_type="dm",
                                   user_id=user_id, user_name="probe")
        self._rec("inject", chat=str(chat_id), mid=message_id,
                  profile=getattr(source, "profile", None))
        await self.handle_message(MessageEvent(text=text, message_type=MessageType.TEXT,
                                               source=source, message_id=message_id))


def _validate(config):
    return bool(get_scoped_secret("ALICE_PATH_SECRET"))


def register(ctx):
    ctx.register_platform(
        name=PLATFORM,
        label="Yandex Alice (S2 probe)",
        adapter_factory=ProbeAdapter,
        check_fn=lambda: True,
        validate_config=_validate if __GATE__ else None,
        allowed_users_env="ALICE_OWNER_USER_ID",
        allowlist_suppresses_pairing=__SUPPRESS__,
        platform_hint="HINT-PLUGIN-MARK: voice channel, answer briefly.",
        display_tier="minimal",
    )
'''


# --- helpers (stdlib only) -------------------------------------------------------------------


def _rss_mb() -> int:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def _last_user_text(body: dict) -> str:
    for msg in reversed(body.get("messages") or []):
        if msg.get("role") != "user":
            continue
        return _text_of(msg.get("content"))
    return ""


def _text_of(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content or "")


def _system_text(body: dict) -> str:
    return "\n".join(_text_of(m.get("content")) for m in body.get("messages") or []
                     if m.get("role") == "system")


def _tool_names(body: dict) -> list[str]:
    return sorted(str((t.get("function") or {}).get("name")) for t in body.get("tools") or [])


def is_home_notice(op: dict) -> bool:
    return (str(op.get("content", "")).startswith("📬") and op.get("reply_to") is None
            and op.get("notify") is not True)


class Script:
    """FakeLLMServer responder keyed by a ``[token]`` in the latest user message."""

    def __init__(self, text_cls) -> None:
        self._text_cls = text_cls
        self.queues: dict[str, list] = {}
        self.seen: list[str] = []

    def add(self, token: str, *responses) -> None:
        self.queues.setdefault(token, []).extend(responses)

    def __call__(self, record: dict):
        text = _last_user_text(record.get("body") or {})
        for token, queue in self.queues.items():
            if f"[{token}]" in text:
                self.seen.append(token)
                return queue.pop(0) if queue else self._text_cls(f"default {token}")
        self.seen.append("?")
        return self._text_cls("unscripted")


def _insert_into_agent(cfg: Path, lines: str) -> None:
    """Add keys to the ``agent:`` section write_hermes_home() writes (no duplicate top-level key)."""
    text = cfg.read_text(encoding="utf-8")
    if text.count("\nagent:\n") != 1:
        raise AssertionError(f"expected exactly one agent: section in {cfg}")
    cfg.write_text(text.replace("\nagent:\n", "\nagent:\n" + lines, 1), encoding="utf-8")


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def _write_plugin(plugins_dir: Path, suppress: bool, gate: bool) -> None:
    target = plugins_dir / PLUGIN_NAME
    target.mkdir(parents=True, exist_ok=True)
    (target / "plugin.yaml").write_text(PLUGIN_YAML, encoding="utf-8")
    source = PLUGIN_INIT.replace("__SUPPRESS__", repr(bool(suppress))).replace("__GATE__", repr(bool(gate)))
    (target / "__init__.py").write_text(source, encoding="utf-8")


def _sessions(db: Path) -> Optional[list[tuple]]:
    if not db.exists():
        return None
    con = sqlite3.connect(str(db))
    try:
        return [tuple(r) for r in con.execute("SELECT source, chat_id, session_key FROM sessions")]
    finally:
        con.close()


# --- the gateway world -----------------------------------------------------------------------


def _guard_temp_home() -> Path:
    """Refuse to run against a real Hermes home (harness reads HERMES_HOME set by run.sh)."""
    import pwd  # POSIX only

    home = Path(os.environ.get("HERMES_HOME", "")).resolve()
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if not str(home) or real_home == home or real_home in home.parents:
        raise SystemExit(f"refusing: HERMES_HOME {home} is inside the real home of this user")
    if not any(p.name.startswith("hya-s2-") for p in home.parents):
        raise SystemExit(f"refusing: HERMES_HOME {home} is not under a hya-s2-* temp dir")
    return home


class World:
    """One gateway (default + p2) and one scripted LLM for one group."""

    def __init__(self, group: str) -> None:
        self.group = group
        self.cfg = GROUPS[group]
        self.harness: dict[str, Any] = {}
        self.errors: dict[str, str] = {}
        self.adapters: dict[str, Any] = {}  # owning profile -> adapter instance
        self.dbs: dict[str, Optional[list[tuple]]] = {}
        self.requests: list[dict] = []
        self.rss_peak_mb = 0
        self.home = Path()

    # -- setup --

    def _default_extra(self, port: int) -> str:
        return (
            "gateway:\n"
            "  multiplex_profiles: true\n"
            "  loop_watchdog: false\n"
            "  write_sessions_json: false\n"
            "  profile_routes:\n"
            "    - name: alice-probe\n"
            f"      platform: {PLATFORM}\n"
            f"      profile: {P2}\n"
            "plugins:\n"
            "  enabled:\n"
            f"    - {PLUGIN_NAME}\n"
            "platforms:\n"
            f"  {PLATFORM}:\n"
            "    enabled: true\n"
            "    extra:\n"
            f"      port: {port}\n"
            + ("      unauthorized_dm_behavior: ignore\n" if self.cfg["dm_ignore"] else "")
            + "platform_toolsets:\n"
            f"  {PLATFORM}: [cronjob]\n"
            "display:\n"
            "  busy_input_mode: interrupt\n"
            + TOOLS_EAGER
        )

    def _p2_extra(self, port: int) -> str:
        text = f"platform_toolsets:\n  {PLATFORM}: [todo]\n" + TOOLS_EAGER
        text += "display:\n  busy_input_mode: interrupt\n"
        if self.cfg["p2_overrides"]:
            # The keys display_tier "minimal" would set; platform_hints is a TOP-LEVEL key
            # (agent/agent_init.py reads it from the whole config, not from agent:).
            text += (f"  platforms:\n    {PLATFORM}:\n      tool_progress: 'off'\n"
                     "      interim_assistant_messages: false\n      streaming: false\n"
                     f"platform_hints:\n  {PLATFORM}:\n    append: {HINT_CONFIG}\n")
        if self.cfg["plugin_in_p2"]:
            text += f"plugins:\n  enabled:\n    - {PLUGIN_NAME}\n"
        if self.cfg["enable_in_p2"]:
            text += f"platforms:\n  {PLATFORM}:\n    enabled: true\n    extra:\n      port: {port}\n"
        return text

    def _write_homes(self, base_url: str) -> None:
        from tests.fakes.fake_llm_provider import write_hermes_home

        port = int(os.environ["S2_PROBE_PORT"])
        home, p2 = self.home, self.home / "profiles" / P2
        write_hermes_home(home, base_url, extra_config=self._default_extra(port))
        write_hermes_home(p2, base_url, extra_config=self._p2_extra(port))
        _insert_into_agent(home / "config.yaml", "  auto_recovery_cycles: 0\n")
        _insert_into_agent(p2 / "config.yaml", "  auto_recovery_cycles: 0\n")
        _append(home / ".env", f"{OWNER_ENV}={OWNER_ID}\n{SECRET_ENV}=from-default\n"
                               f"{PATH_SECRET_ENV}=probe-path-secret\n")
        _append(p2 / ".env", f"{SECRET_ENV}=from-p2\n"
                + (f"{PATH_SECRET_ENV}=probe-path-secret-p2\n" if self.cfg.get("p2_path_secret") else ""))
        (home / "SOUL.md").write_text(f"You are the probe agent {SOUL_DEFAULT}.\n", encoding="utf-8")
        (p2 / "SOUL.md").write_text(f"You are the probe agent {SOUL_P2}.\n", encoding="utf-8")
        _write_plugin(home / "plugins", self.cfg["suppress"], self.cfg["gate"])
        if self.cfg["plugin_in_p2"]:
            _write_plugin(p2 / "plugins", self.cfg["suppress"], self.cfg["gate"])

    def _start_rss_watchdog(self) -> None:
        limit = int(os.environ.get("S2_RSS_LIMIT_MB") or 0)

        def loop() -> None:
            while True:
                rss = _rss_mb()
                self.rss_peak_mb = max(self.rss_peak_mb, rss)
                if limit and rss > limit:
                    print(f"\n!!! RSS {rss} MB > limit {limit} MB - probe aborted", flush=True)
                    os._exit(137)
                time.sleep(0.5)

        threading.Thread(target=loop, name="s2-rss-watchdog", daemon=True).start()

    # -- run --

    def run(self, scenarios) -> None:
        self.home = _guard_temp_home()
        self._start_rss_watchdog()
        asyncio.run(self._main(scenarios))
        self.dbs = {"default": _sessions(self.home / "state.db"),
                    P2: _sessions(self.home / "profiles" / P2 / "state.db")}

    async def _main(self, scenarios) -> None:
        # Imported before gateway.run on purpose: both homes (and the default .env) must exist
        # before gateway.run's import-time .env load.
        from tests.fakes.fake_llm_provider import FakeLLMServer, Text

        self.harness["env_before_gateway_import"] = OWNER_ENV in os.environ
        self.script = Script(Text)
        llm = FakeLLMServer(self.script)
        llm.start()
        try:
            self._write_homes(llm.base_url)
            await self._run_gateway(scenarios)
        finally:
            llm.stop()
            self.requests = [r["body"] for r in llm.requests if r.get("kind") == "main"]

    async def _run_gateway(self, scenarios) -> None:
        from gateway.config import Platform, load_gateway_config
        from gateway.run import GatewayRunner, _multiplex_profile_homes
        from agent.secret_scope import is_multiplex_active

        self.harness["env_after_gateway_import"] = OWNER_ENV in os.environ
        runner = GatewayRunner(load_gateway_config())
        if not await runner.start():
            raise AssertionError("GatewayRunner.start() returned False")
        try:
            plat = Platform(PLATFORM)
            primary = runner.adapters.get(plat)
            if primary is not None:
                self.adapters["default"] = primary
            for name, pmap in (getattr(runner, "_profile_adapters", None) or {}).items():
                if isinstance(pmap, dict) and pmap.get(plat) is not None:
                    self.adapters[name] = pmap[plat]
            self.harness.update(
                multiplex_config=getattr(runner.config, "multiplex_profiles", None),
                multiplex_active=is_multiplex_active(),
                served=sorted(name for name, _home in _multiplex_profile_homes(runner.config)),
                failed_platforms=sorted(str(k) for k in (getattr(runner, "_failed_platforms", None) or {})),
            )
            self.primary = primary
            for name, coro_fn in scenarios:
                try:
                    if primary is None:
                        raise AssertionError("no default-profile yandex_alice adapter")
                    await asyncio.wait_for(coro_fn(self), timeout=TURN_TIMEOUT * 2)
                except Exception as exc:  # recorded, asserted by the test of that scenario
                    self.errors[name] = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(SETTLE)
        finally:
            await runner.stop()

    # -- scenario helpers --

    async def wait_complete(self, mid: str, timeout: float = TURN_TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(o["op"] == "complete" and o.get("mid") == mid for o in self.primary.probe_ops):
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"turn {mid} did not complete in {timeout:.0f}s")

    async def turn(self, chat: str, mid: str, token: str, user: str = OWNER_ID) -> None:
        await self.primary.probe_inject(f"probe [{token}] вопрос", mid, chat, user)
        await self.wait_complete(mid)

    # -- views --

    def ops(self, owner: str = "default", op: Optional[str] = None, **match) -> list[dict]:
        adapter = self.adapters.get(owner)
        rows = list(getattr(adapter, "probe_ops", []) or [])
        return [o for o in rows if (op is None or o["op"] == op)
                and all(o.get(k) == v for k, v in match.items())]

    def finals(self, mid: str) -> list[str]:
        return [o["content"] for o in self.ops(op="send", reply_to=mid)
                if o.get("notify") is True and not o.get("interim")]

    def reqs(self, token: str) -> list[dict]:
        return [b for b in self.requests if f"[{token}]" in _last_user_text(b)]

    def dump(self) -> None:
        print(f"\n=== group {self.group}: cfg={self.cfg}")
        print(f"harness={json.dumps(self.harness, ensure_ascii=False)} errors={self.errors}")
        print(f"rss_peak_mb={self.rss_peak_mb} adapters={sorted(self.adapters)}")
        t0 = min((o["t"] for a in self.adapters.values() for o in a.probe_ops), default=0.0)
        for owner, adapter in sorted(self.adapters.items()):
            print(f"--- ops of {owner} adapter")
            for o in adapter.probe_ops:
                d = {k: v for k, v in o.items() if k != "t"}
                d["t"] = round(o["t"] - t0, 3)
                if "content" in d:
                    d["content"] = d["content"][:80]
                print(json.dumps(d, ensure_ascii=False))
        print(f"--- sessions: {json.dumps(self.dbs, ensure_ascii=False)}")
        for token in sorted(set(self.script.seen) - {"?"}):
            for i, body in enumerate(self.reqs(token)):
                system = _system_text(body)
                print(json.dumps({"token": token, "req": i, "tools": _tool_names(body),
                                  "messages": len(body.get("messages") or []),
                                  "soul_p2": SOUL_P2 in system, "soul_default": SOUL_DEFAULT in system,
                                  "hint_plugin": HINT_PLUGIN in system,
                                  "hint_config": HINT_CONFIG in system}, ensure_ascii=False))


def _fakes():
    from tests.fakes import fake_llm_provider as f
    return f


# --- scenarios -------------------------------------------------------------------------------


async def sc_text(w: World) -> None:
    w.script.add("t01", _fakes().Text("Ответ первый."))
    await w.turn("c01", "m01", "t01")


async def sc_history(w: World) -> None:
    w.script.add("t02", _fakes().Text("Ответ второй."))
    await w.turn("c01", "m02", "t02")


async def sc_tool(w: World) -> None:
    f = _fakes()
    w.script.add("t03",
                 f.ToolCall(TOOL_P2, {"todos": [{"id": "1", "content": "проба", "status": "pending"}]},
                            text="Сейчас запишу."),
                 f.Text("Готово."))
    await w.turn("c03", "m03", "t03")


async def sc_foreign(w: World) -> None:
    w.script.add("t04", _fakes().Text("Чужой ответ."))
    await w.primary.probe_inject("probe [t04] вопрос", "m04", "c04", STRANGER_ID)
    await asyncio.sleep(FOREIGN_WAIT)


SCENARIOS_ROUTED = [("text", sc_text), ("history", sc_history), ("tool", sc_tool),
                    ("foreign", sc_foreign)]


# --- tests -----------------------------------------------------------------------------------


class _GroupBase(unittest.TestCase):
    GROUP = ""
    SCENARIOS: list = []
    world: World

    @classmethod
    def setUpClass(cls) -> None:
        cls.world = World(cls.GROUP)
        cls.world.run(cls.SCENARIOS)
        cls.world.dump()

    def no_error(self, name: str) -> None:
        self.assertNotIn(name, self.world.errors, self.world.errors.get(name))

    def test_00_harness(self) -> None:
        """The harness itself: multiplexing really on, default .env loaded, served = {default, p2}.
        If this fails, nothing below is a statement about Hermes."""
        h = self.world.harness
        self.assertIs(h.get("multiplex_config"), True)
        self.assertIs(h.get("multiplex_active"), True)
        self.assertIs(h.get("env_after_gateway_import"), True, "default .env not in os.environ")
        self.assertEqual(h.get("served"), ["default", P2])


class _RoutedTests(_GroupBase):
    SCENARIOS = SCENARIOS_ROUTED
    HINT: Optional[str] = None  # marker that MUST be in p2's prompt; None = must be absent
    MINIMAL = False  # True: no commentary/progress in a tool turn (display tier minimal in p2)
    ADAPTERS = ["default"]  # profiles that end up with a yandex_alice adapter
    STRANGER = "pairing"  # what a stranger gets back: "pairing" (a pairing code) or "none"

    def test_01_boot(self) -> None:
        """The default adapter connects once and binds; which profiles built an adapter is pinned."""
        w = self.world
        self.assertEqual(sorted(w.adapters), self.ADAPTERS)
        connects = w.ops(op="connect")
        self.assertEqual([c["bound"] for c in connects], ["ok"])
        self.assertEqual(Path(w.ops(op="init")[0]["home"]).resolve(), w.home)

    def test_02_routed_turn(self) -> None:
        w = self.world
        self.no_error("text")
        starts = w.ops(op="start", mid="m01")
        self.assertEqual([s["profile"] for s in starts], [P2])
        self.assertEqual(w.finals("m01"), ["Ответ первый."])
        body = w.reqs("t01")[0]
        system = _system_text(body)
        self.assertIn(SOUL_P2, system)
        self.assertNotIn(SOUL_DEFAULT, system)
        self.assertIn(TOOL_P2, _tool_names(body))
        self.assertNotIn(TOOL_DEFAULT, _tool_names(body))
        if self.HINT is not None:
            self.assertIn(self.HINT, system)
        else:
            self.assertNotIn(HINT_PLUGIN, system)
            self.assertNotIn(HINT_CONFIG, system)

    def test_03_tool_turn(self) -> None:
        w = self.world
        self.no_error("tool")
        self.assertEqual(w.finals("m03"), ["Готово."])
        extra = [o["content"][:60] for o in w.ops(op="send", chat="c03")
                 if not is_home_notice(o) and o["content"] != "Готово."]
        print(f"\n[{self.GROUP}] tool turn, non-final sends: {extra}")
        if self.MINIMAL:
            self.assertEqual(extra, [])
        else:  # the generic plugin tier: commentary always; the 📋 progress line only sometimes
            self.assertEqual(extra[:1], ["Сейчас запишу."])
            self.assertTrue(all(e == "Сейчас запишу." or e.startswith("📋") for e in extra), extra)

    def test_04_state_db(self) -> None:
        w = self.world
        p2_rows = [r for r in (w.dbs.get(P2) or []) if r[0] == PLATFORM]
        default_rows = [r for r in (w.dbs.get("default") or []) if r[0] == PLATFORM]
        self.assertIn("c01", [r[1] for r in p2_rows])
        self.assertEqual(default_rows, [])

    def test_05_history(self) -> None:
        w = self.world
        self.no_error("history")
        self.assertEqual(w.finals("m02"), ["Ответ второй."])
        body = w.reqs("t02")[0]
        users = [_text_of(m.get("content")) for m in body.get("messages") or [] if m.get("role") == "user"]
        self.assertTrue(any("[t01]" in u for u in users), users)

    def test_06_secret_scope(self) -> None:
        """Expected: from-default at construction and connect (unscoped default). Observed: in send()."""
        w = self.world
        self.assertEqual([o["secret"] for o in w.ops(op="init")], ["from-default"])
        self.assertEqual([o["secret"] for o in w.ops(op="connect")], ["from-default"])
        during = sorted({(o["secret"], o["home"].rsplit("/", 2)[-1]) for o in w.ops(op="send", chat="c01")})
        print(f"\n[{self.GROUP}] get_scoped_secret / home in send() of a routed turn: {during}")
        self.assertTrue(during)

    def test_07_foreign_user(self) -> None:
        """No agent turn for a stranger. Pinned: the processing hooks DO fire (start/complete in p2),
        and with allowlist_suppresses_pairing=True alone the stranger still gets a pairing code - the
        reply is built in p2's scope, where the owner allowlist (default's .env) is not visible."""
        w = self.world
        self.no_error("foreign")
        self.assertNotIn("t04", w.script.seen)
        hooks = [(o["op"], o.get("profile"), o.get("outcome")) for o in w.ops(chat="c04")
                 if o["op"] in ("start", "complete")]
        sends = [(o["content"][:120], o["home"].rsplit("/", 2)[-1]) for o in w.ops(op="send", chat="c04")]
        print(f"\n[{self.GROUP}] stranger: hooks={hooks} sends={sends}")
        self.assertEqual(hooks, [("start", P2, None), ("complete", P2, "success")])
        if self.STRANGER == "none":
            self.assertEqual(sends, [])
        else:
            self.assertEqual(len(sends), 1)
            self.assertIn("pairing code", sends[0][0])


class GroupA1(_RoutedTests):
    """Plugin only in the default profile."""

    GROUP = "A1"


class GroupA2(_RoutedTests):
    """Plugin also in profiles/p2/plugins + p2's plugins.enabled; platform enabled in default only.
    Pinned: with no validate_config, is_connected falls back to check_fn and p2 enables the platform
    by itself - a second adapter and a second listener."""

    GROUP = "A2"
    HINT = HINT_PLUGIN
    MINIMAL = True
    ADAPTERS = ["default", P2]


class GroupA2g(_RoutedTests):
    """A2 + validate_config requiring ALICE_PATH_SECRET in the current scope (only default has it)."""

    GROUP = "A2g"
    HINT = HINT_PLUGIN
    MINIMAL = True


class GroupA3(_RoutedTests):
    """Plugin only in default; p2 sets platform_hints + display.platforms for yandex_alice."""

    GROUP = "A3"
    HINT = HINT_CONFIG
    MINIMAL = True


class GroupA4(_RoutedTests):
    """A1 + default's platforms.yandex_alice.extra.unauthorized_dm_behavior: ignore."""

    GROUP = "A4"
    STRANGER = "none"


class GroupA0(_GroupBase):
    """allowlist_suppresses_pairing=False: what a stranger gets back."""

    GROUP = "A0"
    SCENARIOS = [("foreign", sc_foreign)]

    def test_07_foreign_user(self) -> None:
        w = self.world
        self.no_error("foreign")
        self.assertNotIn("t04", w.script.seen)
        hooks = [(o["op"], o.get("profile"), o.get("outcome")) for o in w.ops(chat="c04")
                 if o["op"] in ("start", "complete")]
        sends = [(o["content"][:120], o["home"].rsplit("/", 2)[-1]) for o in w.ops(op="send", chat="c04")]
        print(f"\n[A0] stranger without the flag: hooks={hooks} sends={sends}")


class GroupB(_GroupBase):
    """Plugin AND platforms.yandex_alice enabled in p2 too: what the gateway builds and binds."""

    GROUP = "B"
    SCENARIOS = [("text", sc_text)]

    def test_10_double_enable(self) -> None:
        """Pinned: p2 builds its own adapter, whose connect() hits the port the default already holds;
        the gateway logs "yandex_alice failed to connect (profile: p2)" and keeps no p2 adapter -
        the default adapter alone serves the routed turn. Nothing stops the second bind attempt."""
        w = self.world
        self.no_error("text")
        self.assertEqual(sorted(w.adapters), ["default"])
        self.assertEqual([c["bound"] for c in w.ops(op="connect")], ["ok"])
        self.assertEqual([s["profile"] for s in w.ops(op="start", mid="m01")], [P2])
        summary = {owner: {"connects": [c["bound"] for c in w.ops(owner, op="connect")],
                           "starts": [(s.get("mid"), s.get("profile")) for s in w.ops(owner, op="start")]}
                   for owner in sorted(w.adapters)}
        print(f"\n[B] adapters: {json.dumps(summary, ensure_ascii=False)} "
              f"failed={w.harness.get('failed_platforms')} errors={w.errors}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
