"""Prototype S6: the voice platform's gateway session and cron jobs created by voice.

What it answers (``docs/PLAN.md``, stage 1, S6): can a ``kind: platform`` plugin living in the DEFAULT profile
of a multiplexing gateway, through the public plugin API only, (a) rely on the gateway session as the history
of a conversation - its key and what context compression does to it; (b) get the result of a cron job
created by voice to Telegram instead of losing it on the ``origin`` address (the voice channel never speaks
first); (c) forward other out-of-turn sends to Telegram as ``Алиса: …``; (d) close a turn without ``clarify``
when the agent asks back?

How: a REAL ``GatewayRunner`` with ``multiplex_profiles`` - the default profile (the probe plugin, a REAL
Telegram adapter on Hermes' Bot API stand-in ``tests/fakes/platforms/telegram_standin``) and ``p2``, the route
target of ``yandex_alice`` and of the owner's Telegram chat (like a real target profile without a bot of its
own) - plus the gateway's own cron ticker and housekeeping threads; the agent is Hermes' scripted loopback LLM.
Groups: A - the plugin only in the default profile; B - also in ``p2`` with ``ALICE_TELEGRAM_CHAT`` in its
``.env`` (S2's A2g layout); C - also in ``p2`` without that chat.

PROBE ONLY. ``gateway.run``, ``gateway.run_bootstrap``, ``tests.*`` and the runner's private attributes are the
harness, NOT the plugin API; the plugin (``plugin/``) imports only ``public-api.md`` modules. Run through
``run.sh``. Pure-layer tests need no Hermes:
``cd prototypes/s6 && python -m unittest probe_s6 -k GuardTests -k ForwardTests``. Python >= 3.11.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
CHAT = "111"  # synthetic Telegram chat id
TARGET = "telegram:" + CHAT
PLATFORM = "yandex_alice"
PLUGIN_NAME = "yandex-alice-s6"
MODULE_PREFIX = "hermes_plugins.yandex_alice_s6"
P2 = "p2"
OWNER_ID = "u-probe-owner"       # Alice user id of the owner (synthetic)
TG_OWNER = int(CHAT)             # the owner's private chat; routed to p2 like a real target profile's chat
TG_UNROUTED = 444                # a chat no route maps to p2
# Synthetic, deliberately NOT in the Telegram token format (keeps secret scanners quiet).
TOKEN_DEFAULT = "100001:s6-default-bot"
SECRET = "s6-synthetic-path-secret-0123456789"
TOOLS_EAGER = "tools:\n  tool_search:\n    enabled: 'off'\n"
BIG_TOKENS = 125_000             # > 0.85 x the 128k window the fake model reports: forces compression
TURN_TIMEOUT = 90.0
CRON_WAIT = 330.0                # one-shot "in 1m" + a 60 s tick + a 60 s drain, with headroom
SETTLE = 1.0
GROUPS: dict[str, dict[str, bool]] = {
    "A": dict(plugin_p2=False, chat_p2=False),
    "B": dict(plugin_p2=True, chat_p2=True),
    "C": dict(plugin_p2=True, chat_p2=False),
}


def _load(name: str):
    spec = importlib.util.spec_from_file_location("s6_" + name, HERE / "plugin" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- the pure layer, no Hermes ---------------------------------------------------------------------


class GuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.g = _load("cron_guard")

    def guard(self, **args):
        return self.g.guard_args(args, CHAT)

    def test_create_without_deliver_goes_to_telegram(self) -> None:
        self.assertEqual(self.guard(action="create", schedule="in 1m", prompt="p"), {"deliver": TARGET})

    def test_alice_addresses_are_replaced(self) -> None:
        for value in ("origin", " Origin ", "yandex_alice", "yandex_alice:chat-1", "YANDEX_ALICE:x"):
            self.assertEqual(self.guard(action="create", deliver=value), {"deliver": TARGET}, value)

    def test_explicit_choice_stays(self) -> None:
        for value in ("local", "telegram:222", TARGET, "all", "bot-chat:p2", "slack:#x"):
            self.assertIsNone(self.guard(action="create", deliver=value), value)

    def test_lists_replace_only_alice_tokens(self) -> None:
        self.assertEqual(self.guard(action="create", deliver="origin,local"), {"deliver": TARGET + ",local"})
        self.assertEqual(self.guard(action="create", deliver=["local", "origin"]), {"deliver": "local," + TARGET})
        self.assertEqual(self.guard(action="create", deliver="origin, yandex_alice:x," + TARGET), {"deliver": TARGET})

    def test_update_without_deliver_keeps_the_old_target(self) -> None:
        # On update an omitted deliver means "do not change" (a job made in Telegram with deliver=local stays).
        self.assertIsNone(self.guard(action="update", job_id="j1", schedule="every 2h"))
        self.assertIsNone(self.guard(action="update", job_id="j1", deliver=""))
        self.assertEqual(self.guard(action="update", job_id="j1", deliver="origin"), {"deliver": TARGET})

    def test_failure_deliver_only_explicit_alice(self) -> None:
        self.assertEqual(self.guard(action="create", deliver="local", failure_deliver="origin"),
                         {"failure_deliver": TARGET})
        self.assertEqual(self.guard(action="create", failure_deliver="yandex_alice"),
                         {"deliver": TARGET, "failure_deliver": TARGET})
        self.assertEqual(self.guard(action="create", deliver="local", failure_deliver=""), None)
        self.assertEqual(self.guard(action="update", job_id="j1", failure_deliver="origin"),
                         {"failure_deliver": TARGET})

    def test_other_actions_untouched(self) -> None:
        for action in ("list", "run", "pause", "resume", "remove", "cancel", None, 5):
            self.assertIsNone(self.guard(action=action, deliver="origin", job_id="j1"), action)

    def test_no_chat_no_rewrite(self) -> None:
        for chat in ("", "  ", None, "1,2", "1 2", "a:b"):
            self.assertIsNone(self.g.guard_args({"action": "create", "deliver": "origin"}, chat), chat)

    def test_garbage_never_raises(self) -> None:
        # A raising pre_tool_call hook blocks every tool of the profile: garbage must give None, not an error.
        for args in (None, [], "create", 5, {"action": ["create"]}, {"action": "create", "deliver": 5},
                     {"action": "create", "deliver": ["origin", 5]}, {"action": "update", "failure_deliver": {}}):
            self.assertIsNone(self.g.guard_args(args, CHAT), args)

    def test_action_is_normalized_like_hermes(self) -> None:
        # tools/cronjob_tools.py: (action or "").strip().lower()
        self.assertEqual(self.guard(action=" CREATE ", prompt="p"), {"deliver": TARGET})
        self.assertEqual(self.guard(action="Update", job_id="j1", deliver="origin"), {"deliver": TARGET})


class ForwardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.r = _load("recovery")

    def fwd(self, open_turn, kind, text="Sorry, I encountered an error."):
        return self.r.forward_text(open_turn=open_turn, kind=kind, text=text)

    def test_outside_a_turn_goes_to_telegram_with_the_label(self) -> None:
        self.assertEqual(self.fwd(False, self.r.OUTSIDE, "Текст"), "Алиса:\n\nТекст")

    def test_inside_a_turn_nothing_is_forwarded(self) -> None:
        # 📬 No home channel…, tool progress, retry notices: neither voiced nor duplicated to Telegram
        for kind in (self.r.OUTSIDE, self.r.INTERIM, self.r.FINAL):
            self.assertIsNone(self.fwd(True, kind, "📬 No home channel is set"), kind)

    def test_service_recovered_and_final_are_not_forwarded(self) -> None:
        for kind in (self.r.INTERIM, self.r.RECOVERED, self.r.FINAL):
            self.assertIsNone(self.fwd(False, kind), kind)

    def test_empty_text_is_not_forwarded(self) -> None:
        for text in ("", "  \n", None):
            self.assertIsNone(self.fwd(False, self.r.OUTSIDE, text), repr(text))

    def test_classify_then_forward(self) -> None:
        kind = self.r.classify_send(open_turn=False, turn_mid=None, reply_to=None, notify=None, interim=False,
                                    text="Sorry, I encountered an error.")
        self.assertEqual(kind, self.r.OUTSIDE)
        marked = "♻️ Recovered reply — may be a duplicate:\n\nОтвет"
        kind = self.r.classify_send(open_turn=False, turn_mid=None, reply_to=None, notify=None, interim=False,
                                    text=marked)
        self.assertIsNone(self.fwd(False, kind, marked))


# --- harness (Hermes) ------------------------------------------------------------------------------


def _rss_mb() -> int:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def _guard_temp_home() -> Path:
    import pwd  # POSIX only

    raw = os.environ.get("HERMES_HOME", "").strip()
    if not raw:
        raise SystemExit("refusing: HERMES_HOME is not set (run through run.sh)")
    home = Path(raw).resolve()
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if real_home == home or real_home in home.parents:
        raise SystemExit(f"refusing: HERMES_HOME {home} is inside the real home of this user")
    if not any(p.name.startswith("hya-s6-") for p in home.parents):
        raise SystemExit(f"refusing: HERMES_HOME {home} is not under a hya-s6-* temp dir")
    return home


def _text_of(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content or "")


def _last_user_text(body: dict) -> str:
    for msg in reversed(body.get("messages") or []):
        if msg.get("role") == "user":
            return _text_of(msg.get("content"))
    return ""


def _all_text(body: dict) -> str:
    return "\n".join(_text_of(m.get("content")) for m in body.get("messages") or [])


def _tool_names(body: dict) -> list[str]:
    return sorted(str((t.get("function") or {}).get("name")) for t in body.get("tools") or [])


def _job_id_from_tools(record: dict) -> Optional[str]:
    for msg in reversed((record.get("body") or {}).get("messages") or []):
        if msg.get("role") == "tool":
            m = re.search(r'"(?:job_id|id)"\s*:\s*"([0-9a-zA-Z_-]{6,})"', _text_of(msg.get("content")))
            if m:
                return m.group(1)
    return None


class Script:
    """FakeLLMServer responder keyed by a ``[token]`` in the latest user message; a queue item may be a
    callable ``record -> response`` (to use a job id the previous tool call returned)."""

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
                item = queue.pop(0) if queue else self._text_cls(f"default {token}")
                return item(record) if callable(item) else item
        self.seen.append("?")
        return self._text_cls("unscripted")


def _insert_into_agent(cfg: Path, lines: str) -> None:
    text = cfg.read_text(encoding="utf-8")
    if text.count("\nagent:\n") != 1:
        raise AssertionError(f"expected exactly one agent: section in {cfg}")
    cfg.write_text(text.replace("\nagent:\n", "\nagent:\n" + lines, 1), encoding="utf-8")


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def _fakes():
    from tests.fakes import fake_llm_provider as f
    return f


def _ro(path: Path) -> Optional[sqlite3.Connection]:
    if not path.exists():
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


class World:
    """One gateway (default + p2), one scripted LLM, one Bot API stand-in, one group."""

    def __init__(self, group: str) -> None:
        self.group = group
        self.cfg = GROUPS[group]
        self.results: dict[str, Any] = {}
        self.errors: dict[str, str] = {}
        self.harness: dict[str, Any] = {}
        self.rss_peak_mb = 0
        self.big_tokens = False
        self.home = Path()
        self.validate: Optional[dict] = None

    # -- setup --

    @property
    def p2(self) -> Path:
        return self.home / "profiles" / P2

    def _write_homes(self, base_url: str) -> None:
        f = _fakes()
        home, p2, t = self.home, self.p2, self.tg
        f.write_hermes_home(home, base_url, extra_config=(
            "gateway:\n  multiplex_profiles: true\n  loop_watchdog: false\n"
            "  profile_routes:\n"
            f"    - name: alice-probe\n      platform: {PLATFORM}\n      profile: {P2}\n"
            f"    - name: tg-probe\n      platform: telegram\n      chat_id: '{TG_OWNER}'\n      profile: {P2}\n"
            f"plugins:\n  enabled:\n    - {PLUGIN_NAME}\n"
            f"platforms:\n  {PLATFORM}:\n    enabled: true\n    extra:\n      telegram_base_url: {t.api_base}\n"
            f"  telegram:\n    enabled: true\n    extra:\n      base_url: {t.api_base}\n"
            f"      base_file_url: {t.file_base}\n" + TOOLS_EAGER))
        p2_plugins = f"plugins:\n  enabled:\n    - {PLUGIN_NAME}\n" if self.cfg["plugin_p2"] else ""
        f.write_hermes_home(p2, base_url, extra_config=(
            f"platform_toolsets:\n  {PLATFORM}: [cronjob, todo]\n  telegram: [cronjob, todo]\n"
            "compression:\n  protect_last_n: 2\n" + p2_plugins + TOOLS_EAGER))
        for h in (home, p2):
            _insert_into_agent(h / "config.yaml", "  auto_recovery_cycles: 0\n")
        _append(home / ".env", f"ALICE_PATH_SECRET={SECRET}\nALICE_OWNER_USER_ID={OWNER_ID}\n"
                f"ALICE_TELEGRAM_CHAT={TG_OWNER}\nTELEGRAM_BOT_TOKEN={TOKEN_DEFAULT}\n"
                f"TELEGRAM_ALLOWED_USERS={TG_OWNER}\nHERMES_TELEGRAM_DISABLE_FALLBACK_IPS=1\n")
        _append(p2 / ".env", f"TELEGRAM_ALLOWED_USERS={TG_OWNER}\n"
                + (f"ALICE_TELEGRAM_CHAT={TG_OWNER}\n" if self.cfg["chat_p2"] else ""))
        targets = [home] + ([p2] if self.cfg["plugin_p2"] else [])
        for h in targets:
            shutil.copytree(HERE / "plugin", h / "plugins" / PLUGIN_NAME, ignore=shutil.ignore_patterns("__pycache__"))

    def _start_rss_watchdog(self) -> None:
        limit = int(os.environ.get("S6_RSS_LIMIT_MB") or 0)

        def loop() -> None:
            while True:
                rss = _rss_mb()
                self.rss_peak_mb = max(self.rss_peak_mb, rss)
                if limit and rss > limit:
                    print(f"\n!!! RSS {rss} MB > limit {limit} MB - probe aborted by the watchdog", flush=True)
                    os._exit(137)
                time.sleep(0.5)

        threading.Thread(target=loop, name="s6-rss-watchdog", daemon=True).start()

    # -- run --

    def run(self, scenarios) -> None:
        self.home = _guard_temp_home()
        self._start_rss_watchdog()
        asyncio.run(self._main(scenarios))
        self._validate()

    async def _main(self, scenarios) -> None:
        from tests.fakes.fake_llm_provider import FakeLLMServer, Text
        from tests.fakes.platforms.telegram_standin import TelegramStandin

        self.tg = TelegramStandin(TOKEN_DEFAULT)
        self.tg.start()
        self.script = Script(Text)
        self.llm = FakeLLMServer(self.script, prompt_tokens_fn=self._prompt_tokens)
        self.llm.start()
        try:
            self._write_homes(self.llm.base_url)
            await self._run_gateway(scenarios)
        finally:
            self.llm.stop()
            self.tg.stop()

    def _prompt_tokens(self, body: dict) -> int:
        return BIG_TOKENS if self.big_tokens and "[z" in _all_text(body) else 100

    async def _run_gateway(self, scenarios) -> None:
        from gateway.config import Platform, load_gateway_config
        from gateway.run import GatewayRunner

        runner = GatewayRunner(load_gateway_config())
        if not await runner.start():
            raise AssertionError("GatewayRunner.start() returned False")
        cron_stop = None
        try:
            self.runner = runner
            self.alice = runner.adapters.get(Platform(PLATFORM))
            if self.alice is None:
                raise AssertionError("no default-profile yandex_alice adapter")
            cron_stop = self._start_cron(runner)
            await self.wait(lambda: self.tg.calls_of("getUpdates"), "default bot polling", 60)
            p2map = (getattr(runner, "_profile_adapters", None) or {}).get(P2) or {}
            self.harness.update(
                adapters=sorted(str(getattr(k, "value", k)) for k in runner.adapters),
                p2_adapters=sorted(str(getattr(k, "value", k)) for k in p2map) if isinstance(p2map, dict) else str(p2map),
                failed_platforms=sorted(str(getattr(k, "value", k)) for k in (getattr(runner, "_failed_platforms", None) or {})),
                plugin_modules=sorted(n for n in sys.modules if n.startswith(MODULE_PREFIX) and "." not in n[len(MODULE_PREFIX):]),
                registers=[self._where(r["home"]) for r in self.journal("register")],
                connects=[dict(home=self._where(r["home"]), forward=r["forward_enabled"], reason=r["reason"])
                          for r in self.journal("connect")])
            for name, coro_fn in scenarios:
                try:
                    self.results[name] = await asyncio.wait_for(coro_fn(self), timeout=CRON_WAIT + 120)
                except Exception as exc:  # recorded, asserted by the test of that scenario
                    self.errors[name] = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(SETTLE)
        finally:
            if cron_stop is not None:
                cron_stop.set()
            await runner.stop()

    def _start_cron(self, runner):
        """The cron ticker + housekeeping threads, as ``start_gateway`` starts them (harness only)."""
        from gateway.run_bootstrap import _start_gateway_start_cron_and_housekeeping

        try:
            cron_stop, provider, _cron_thread, _hk = _start_gateway_start_cron_and_housekeeping(runner)
            self.harness["cron"] = dict(provider=type(provider).__name__)
            return cron_stop
        except Exception as exc:  # noqa: BLE001 - recorded; the cron scenes then report no delivery
            self.harness["cron"] = dict(error=f"{type(exc).__name__}: {exc}")
            return None

    def _validate(self) -> None:
        """``hermes plugins validate`` on the probe plugin: imports outside public-api.md."""
        try:
            from hermes_cli.plugin_validate import validate_plugin_dir

            report = validate_plugin_dir(HERE / "plugin", None)
            self.validate = {"ok": bool(report.ok), "warnings": [str(w) for w in report.warnings]}
        except Exception as exc:  # noqa: BLE001
            self.validate = {"error": f"{type(exc).__name__}: {exc}"}

    # -- helpers --

    def _where(self, home: str) -> str:
        if Path(home) == self.home:
            return "default"
        if Path(home) == self.p2:
            return P2
        return "other"

    async def wait(self, pred, what: str, timeout: float = TURN_TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = pred()
            if value:
                return value
            await asyncio.sleep(0.1)
        raise AssertionError(f"timed out waiting for {what} ({timeout:.0f}s)")

    def journal(self, op: Optional[str] = None, **match) -> list[dict]:
        rows = []
        for name, mod in list(sys.modules.items()):
            if name.startswith(MODULE_PREFIX) and "." not in name[len(MODULE_PREFIX):]:
                rows.extend(getattr(mod, "JOURNAL", []) or [])
        rows.sort(key=lambda r: r["t"])
        return [r for r in rows if (op is None or r["op"] == op) and all(r.get(k) == v for k, v in match.items())]

    async def turn(self, chat: str, mid: str, token: str, text: str = "вопрос", timeout: float = TURN_TIMEOUT) -> dict:
        t0 = time.monotonic()
        await self.alice.probe_inject(f"probe [{token}] {text}", mid, chat)
        done = await self.wait(lambda: next(iter(self.journal("complete", mid=mid)), None), f"turn {mid}", timeout)
        await asyncio.sleep(0.3)
        sends = [r for r in self.journal("send", chat=chat) if r["t"] >= t0]
        return dict(complete=done["outcome"], took=round(done["t"] - t0, 2),
                    finals=[r["head"] for r in sends if r["kind"] == "final" and r["reply_to"] == mid],
                    sends=[{k: r[k] for k in ("kind", "open_turn", "reply_to", "notify", "interim", "forwarded", "head")}
                           for r in sends])

    def bodies(self, token: str) -> list[dict]:
        return [r["body"] for r in list(self.llm.requests) if r.get("kind") == "main"
                and f"[{token}]" in _last_user_text(r.get("body") or {})]

    def tool_results(self, token: str) -> list[str]:
        bodies = self.bodies(token)
        if not bodies:
            return []
        return [_text_of(m.get("content"))[:300] for m in bodies[-1].get("messages") or [] if m.get("role") == "tool"]

    def tg_sends(self, since: float = 0.0) -> list[dict]:
        return [dict(chat=str(c.params.get("chat_id")), text=str(c.params.get("text") or "")[:400],
                     disable_notification=c.params.get("disable_notification"), parse_mode=c.params.get("parse_mode"),
                     faulted=c.faulted)
                for c in self.tg.calls_of("sendMessage") if c.at >= since]

    def sessions(self, chat: str) -> dict:
        """p2's state.db rows of the conversation ``chat``: the routing entry and the session lineage."""
        out: dict[str, Any] = {}
        con = _ro(self.p2 / "state.db")
        if con is None:
            return {"state_db": "missing"}
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            ids: set = set()
            if "gateway_routing" in tables:
                routing = []
                for r in con.execute("SELECT scope, session_key, entry_json FROM gateway_routing"):
                    if str(r["session_key"]).endswith(":" + chat):
                        try:
                            entry = json.loads(r["entry_json"] or "{}")
                        except ValueError:
                            entry = {}
                        sid = entry.get("session_id")
                        ids.add(sid)
                        routing.append(dict(scope=r["scope"], session_key=r["session_key"], session_id=sid))
                out["routing"] = routing
            cols = [r[1] for r in con.execute("PRAGMA table_info(sessions)")]
            want = [c for c in ("id", "parent_session_id", "session_key", "source", "end_reason", "message_count",
                                "transport_profile", "profile_name") if c in cols]
            if "session_key" in cols:
                for r in con.execute("SELECT id FROM sessions WHERE session_key LIKE ?", ("%:" + chat,)):
                    ids.add(r["id"])
            for _ in range(3):  # the compression lineage both ways
                ids.discard(None)
                if not ids:
                    break
                marks = ",".join("?" * len(ids))
                for r in con.execute(f"SELECT id, parent_session_id FROM sessions WHERE id IN ({marks}) "
                                     f"OR parent_session_id IN ({marks})", [*ids, *ids]):
                    ids.add(r["id"])
                    if r["parent_session_id"]:
                        ids.add(r["parent_session_id"])
            ids.discard(None)
            rows = []
            if ids:
                marks = ",".join("?" * len(ids))
                for r in con.execute(f"SELECT {', '.join(want)} FROM sessions WHERE id IN ({marks})", list(ids)):
                    row = dict(r)
                    if "messages" in tables:
                        row["messages"] = con.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?",
                                                      (row["id"],)).fetchone()[0]
                    rows.append(row)
            out["sessions"] = rows
        finally:
            con.close()
        for path in sorted(self.home.rglob("sessions.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            items = data.items() if isinstance(data, dict) else []
            hits = {k: (v.get("session_id") if isinstance(v, dict) else v) for k, v in items if str(k).endswith(":" + chat)}
            if hits:
                out.setdefault("sessions_json", {})[str(path.relative_to(self.home))] = hits
        return out

    def jobs(self) -> dict[str, dict]:
        path = self.p2 / "cron" / "jobs.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        items = data.get("jobs", []) if isinstance(data, dict) else data
        out = {}
        for j in items or []:
            origin = j.get("origin") or {}
            out[str(j.get("name"))] = dict(
                id=j.get("id"), deliver=j.get("deliver"), failure_deliver=j.get("failure_deliver"),
                origin=dict(platform=origin.get("platform"), chat_id=origin.get("chat_id")) if origin else None,
                state=j.get("state"), last_status=j.get("last_status"), last_error=j.get("last_error"),
                last_delivery_error=j.get("last_delivery_error"))
        return out

    def deliveries(self) -> dict:
        con = _ro(self.p2 / "cron" / "deliveries.db")
        if con is None:
            return {"deliveries_db": "missing"}
        out = {}
        try:
            for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                rows = []
                for r in con.execute(f"SELECT * FROM {table} LIMIT 30"):
                    rows.append({k: (len(v) if isinstance(v, (str, bytes)) and len(v) > 80 else v)
                                 for k, v in dict(r).items()})
                out[table] = rows
        finally:
            con.close()
        return out

    async def wait_jobs(self, names: list[str], timeout: float = CRON_WAIT) -> dict:
        terminal = {"ok", "delivery_failed", "error", "blocked_config"}

        def settled():
            jobs = self.jobs()
            return all(jobs.get(n, {}).get("last_status") in terminal for n in names)

        try:
            await self.wait(settled, f"cron jobs {names}", timeout)
        except AssertionError:
            pass
        await asyncio.sleep(2)
        jobs = self.jobs()
        return {n: jobs.get(n) for n in names}

    def dump(self) -> None:
        print(f"\n=== group {self.group}: cfg={self.cfg} rss_peak_mb={self.rss_peak_mb}")
        print(f"harness={json.dumps(self.harness, ensure_ascii=False, default=str)}")
        print(f"errors={self.errors}")
        for row in self.journal():
            if row["op"] in ("hook", "hook_error", "standalone", "forward", "clarify", "connect", "register") or (
                    row["op"] == "start" and row.get("internal")):
                print(json.dumps({k: (self._where(v) if k == "home" else v) for k, v in row.items()},
                                 ensure_ascii=False, default=str))
        for name, res in self.results.items():
            print(f"--- {name}: {json.dumps(res, ensure_ascii=False, default=str)[:6000]}")
        print(f"validate={json.dumps(self.validate, ensure_ascii=False)[:2000]}")


# --- scenarios -------------------------------------------------------------------------------------


def _create(name: str, schedule: str = "in 1m", **extra):
    f = _fakes()
    args = dict(action="create", schedule=schedule, name=name, prompt=f"[job-{name}] Напомни про {name}.")
    args.update(extra)
    return f.ToolCall("cronjob_manage", args)


async def sc_k1(w: World) -> dict:
    f = _fakes()
    w.script.add("k1a", f.Text("Ответ k1a"))
    w.script.add("k1b", f.Text("Ответ k1b"))
    first = await w.turn("chat-k", "m-k1a", "k1a")
    second = await w.turn("chat-k", "m-k1b", "k1b")
    body = (w.bodies("k1b") or [{}])[-1]
    system = next((_text_of(m.get("content")) for m in body.get("messages") or [] if m.get("role") == "system"), "")
    return dict(first=first, second=second, history_has_first="[k1a]" in _all_text(body),
                hint_in_prompt="Voice channel probe" in system, tools=_tool_names(body),
                sessions=w.sessions("chat-k"))


async def sc_k3(w: World) -> dict:
    f = _fakes()
    w.script.add("k3", f.Text("Ответ k3"))
    res = await w.turn("chat-new", "m-k3", "k3")
    body = (w.bodies("k3") or [{}])[-1]
    return dict(turn=res, history_has_k1="[k1a]" in _all_text(body), sessions=w.sessions("chat-new"))


async def sc_z1(w: World) -> dict:
    f = _fakes()
    steps = []
    w.big_tokens = True
    try:
        for i in range(1, 7):
            token = f"z{i}"
            w.script.add(token, f.Text(f"Ответ {token}"))
            aux_before = sum(1 for r in list(w.llm.requests) if r.get("kind") == "aux")
            res = await w.turn("chat-z", f"m-{token}", token)
            snap = w.sessions("chat-z")
            steps.append(dict(turn=res, aux=sum(1 for r in list(w.llm.requests) if r.get("kind") == "aux") - aux_before,
                              sessions=snap))
            if any(s.get("parent_session_id") for s in snap.get("sessions", [])):
                break
    finally:
        w.big_tokens = False
    w.script.add("zafter", f.Text("Ответ после сжатия"))
    after = await w.turn("chat-z", "m-zafter", "zafter")
    body = (w.bodies("zafter") or [{}])[-1]
    text = _all_text(body)
    return dict(steps=steps, after=after, after_sessions=w.sessions("chat-z"),
                after_has_summary="Fake summary" in text, after_has_z1="[z1]" in text,
                after_messages=len(body.get("messages") or []))


async def sc_q2(w: World) -> dict:
    f = _fakes()
    w.script.add("q2", f.Text("Какой жанр вам нравится?"))
    res = await w.turn("chat-q2", "m-q2", "q2")
    return dict(turn=res, clarify=w.journal("clarify", chat="chat-q2"))


async def sc_q3(w: World) -> dict:
    f = _fakes()
    w.script.add("q3", f.ToolCall("clarify", {"question": "Какой жанр?"}), f.Text("Спрошу текстом: какой жанр?"))
    try:
        res = await w.turn("chat-q3", "m-q3", "q3", timeout=60)
        stopped = None
    except AssertionError:
        res = dict(complete=None, waiting=True)
        await w.alice.probe_inject("/stop", "m-q3-stop", "chat-q3")
        await asyncio.sleep(3)
        stopped = [r.get("outcome") for r in w.journal("complete", chat="chat-q3")]
    return dict(turn=res, stopped=stopped, clarify=w.journal("clarify", chat="chat-q3"),
                tool_results=w.tool_results("q3"))


async def sc_cron_wave(w: World) -> dict:
    """C1-C3 in one wave: no deliver (origin), the routed Telegram chat, an unrouted Telegram chat."""
    f = _fakes()
    t0 = time.monotonic()
    plan = {"c1": {}, "c2": dict(deliver=TARGET), "c3": dict(deliver=f"telegram:{TG_UNROUTED}")}
    creates = {}
    for name, extra in plan.items():
        w.script.add(name, _create(name, **extra), f.Text(f"Поставил {name}"))
        w.script.add(f"job-{name}", f.Text(f"Напоминание {name}"))
        creates[name] = await w.turn(f"chat-{name}", f"m-{name}", name)
        creates[name]["tool_results"] = w.tool_results(name)
    jobs = await w.wait_jobs(list(plan))
    outside = [r for r in w.journal("send") if r["t"] >= t0 and not r["open_turn"]]
    return dict(creates=creates, jobs=jobs, deliveries=w.deliveries(), tg=w.tg_sends(t0),
                alice_outside=[{k: r[k] for k in ("chat", "kind", "reply_to", "notify", "meta_keys", "head", "forwarded")}
                               for r in outside],
                standalone=w.journal("standalone"), forwards=[r for r in w.journal("forward") if r["t"] >= t0],
                hooks=[{k: (w._where(v) if k == "home" else v) for k, v in r.items()}
                       for r in w.journal("hook") if r["t"] >= t0])


async def sc_c4(w: World) -> dict:
    """``action=run`` from voice: what comes back into the voice conversation after the background run."""
    f = _fakes()
    w.script.add("c4", _create("c4", schedule="in 30d", deliver=TARGET),
                 lambda rec: f.ToolCall("cronjob_manage", {"action": "run", "job_id": _job_id_from_tools(rec) or "?"}),
                 f.Text("Запустил c4"))
    w.script.add("job-c4", f.Text("Напоминание c4"))
    t0 = time.monotonic()
    res = await w.turn("chat-c4", "m-c4", "c4")
    done = time.monotonic()
    n_llm = len(w.llm.requests)

    def landed():
        return any("c4" in s["text"] for s in w.tg_sends(t0))

    try:
        await w.wait(landed, "the c4 run to reach Telegram", 240)
    except AssertionError:
        pass
    await asyncio.sleep(20)  # a re-entering internal turn, if any, lands after the run
    rows = [r for r in w.journal() if r["t"] >= done and r.get("chat") == "chat-c4"]
    return dict(turn=res, tool_results=w.tool_results("c4"), jobs={"c4": w.jobs().get("c4")}, tg=w.tg_sends(t0),
                after_turn=[{k: r.get(k) for k in ("op", "mid", "internal", "kind", "reply_to", "notify", "head",
                                                    "outcome", "forwarded")} for r in rows],
                llm_after=[_last_user_text(r["body"])[:200] for r in list(w.llm.requests)[n_llm:]
                           if r.get("kind") == "main"][:6])


async def sc_o1(w: World) -> dict:
    """An exception in the handler: the error text goes out after the closed turn -> Telegram, labelled."""
    alice = w.alice
    inner = alice._message_handler  # harness only

    async def boom(event):
        if "[boom]" in (event.text or ""):
            raise RuntimeError("probe boom")
        return await inner(event)

    alice.set_message_handler(boom)
    t0 = time.monotonic()
    try:
        await alice.probe_inject("probe [boom] сбой", "m-boom", "chat-boom")
        await w.wait(lambda: next(iter(w.journal("complete", mid="m-boom")), None), "the boom turn", 30)
        await asyncio.sleep(4)
    finally:
        alice.set_message_handler(inner)
    return dict(sends=[{k: r[k] for k in ("kind", "open_turn", "reply_to", "notify", "meta_keys", "head", "forwarded")}
                       for r in w.journal("send", chat="chat-boom")],
                complete=[r["outcome"] for r in w.journal("complete", chat="chat-boom")],
                forwards=w.journal("forward", chat="chat-boom"), tg=w.tg_sends(t0))


async def sc_h2(w: World) -> dict:
    """Hook rewrites at creation/update (B): the stored deliver/failure_deliver of each variant."""
    f = _fakes()
    variants = {"h2a": {}, "h2b": dict(deliver="origin"), "h2c": dict(deliver="yandex_alice:x"),
                "h2d": dict(deliver="local"), "h2e": dict(deliver=TARGET), "h2f": dict(deliver="origin,local"),
                "h2g": dict(deliver="local", failure_deliver="origin")}
    t0 = time.monotonic()
    for name, extra in variants.items():
        w.script.add(name, _create(name, schedule="in 30d", **extra), f.Text(f"Поставил {name}"))
        await w.turn(f"chat-{name}", f"m-{name}", name)
    w.script.add("h2u", _create("h2u", schedule="in 30d", deliver="local"),
                 lambda rec: f.ToolCall("cronjob_manage", {"action": "update", "job_id": _job_id_from_tools(rec) or "?",
                                                           "schedule": "in 40d"}),
                 f.Text("Обновил h2u"))
    update = await w.turn("chat-h2u", "m-h2u", "h2u")
    jobs = w.jobs()
    return dict(jobs={n: {k: (jobs.get(n) or {}).get(k) for k in ("deliver", "failure_deliver", "origin")}
                      for n in [*variants, "h2u"]},
                update=update, update_tools=w.tool_results("h2u"),
                hooks=[{k: (w._where(v) if k == "home" else v) for k, v in r.items()}
                       for r in w.journal("hook") if r["t"] >= t0])


async def sc_h3(w: World) -> dict:
    """Hook + a real fire: no deliver from voice -> rewritten -> the result reaches Telegram (B)."""
    f = _fakes()
    t0 = time.monotonic()
    w.script.add("h3", _create("h3"), f.Text("Поставил h3"))
    w.script.add("job-h3", f.Text("Напоминание h3"))
    res = await w.turn("chat-h3", "m-h3", "h3")
    jobs = await w.wait_jobs(["h3"])
    return dict(turn=res, jobs=jobs, tg=w.tg_sends(t0), deliveries=w.deliveries(),
                alice_outside=[r["head"] for r in w.journal("send") if r["t"] >= t0 and not r["open_turn"]])


async def sc_h4(w: World) -> dict:
    """What the hook sees in p2 when ALICE_TELEGRAM_CHAT lives only in the default .env (C)."""
    f = _fakes()
    t0 = time.monotonic()
    w.script.add("h4", _create("h4", schedule="in 30d"), f.Text("Поставил h4"))
    res = await w.turn("chat-h4", "m-h4", "h4")
    return dict(turn=res, job=w.jobs().get("h4"),
                hooks=[{k: (w._where(v) if k == "home" else v) for k, v in r.items()}
                       for r in w.journal("hook") if r["t"] >= t0])


async def sc_h5(w: World) -> dict:
    """A Telegram turn of the target profile creates a job: the hook must leave it alone (B)."""
    f = _fakes()
    t0 = time.monotonic()
    w.script.add("h5", _create("h5", schedule="in 30d"), f.Text("Поставил h5 из Telegram"))
    w.tg.dm(TG_OWNER, "probe [h5] напомни")

    def final():
        return next((s for s in w.tg_sends(t0) if "Поставил h5" in s["text"]), None)

    reply = await w.wait(final, "the Telegram reply of h5", TURN_TIMEOUT)
    return dict(reply=reply, job=w.jobs().get("h5"), tool_results=w.tool_results("h5"),
                hooks=[{k: (w._where(v) if k == "home" else v) for k, v in r.items()}
                       for r in w.journal("hook") if r["t"] >= t0])


SCENARIOS_A = [("k1", sc_k1), ("k3", sc_k3), ("z1", sc_z1), ("q2", sc_q2), ("q3", sc_q3), ("o1", sc_o1),
               ("cron", sc_cron_wave), ("c4", sc_c4)]
SCENARIOS_B = [("k1", sc_k1), ("h2", sc_h2), ("h5", sc_h5), ("h3", sc_h3)]
SCENARIOS_C = [("h4", sc_h4)]


# --- groups under unittest -----------------------------------------------------------------------------


class _Group(unittest.TestCase):
    GROUP = ""
    SCENARIOS: list = []
    w: World

    @classmethod
    def setUpClass(cls) -> None:
        cls.w = World(cls.GROUP)
        try:
            cls.w.run(cls.SCENARIOS)
        finally:
            cls.w.dump()

    def res(self, name: str) -> dict:
        self.assertNotIn(name, self.w.errors, self.w.errors.get(name))
        self.assertIn(name, self.w.results)
        return self.w.results[name]

    def test_00_rss_and_harness(self) -> None:
        print(f"rss_peak_mb={self.w.rss_peak_mb} harness={self.w.harness}")
        self.assertGreater(self.w.rss_peak_mb, 0)
        self.assertNotIn("error", self.w.harness.get("cron", {}), self.w.harness.get("cron"))

    def test_validate(self) -> None:
        v = self.w.validate or {}
        print(f"validate={json.dumps(v, ensure_ascii=False)}")
        self.assertNotIn("error", v, v)
        self.assertFalse([x for x in v.get("warnings", []) if "import" in x.lower()], v)

    def assert_no_label_from(self, chats: list[str]) -> None:
        """Strict negative: nothing sent inside these turns was forwarded to Telegram."""
        rows = [r for r in self.w.journal("send") if r["chat"] in chats and r["forwarded"]]
        self.assertEqual(rows, [])
        labelled = [s for s in self.w.tg_sends() if s["text"].startswith("Алиса:")
                    and any(t in s["text"] for t in ("k1a", "k1b", "k3", "Ответ z", "жанр"))]
        self.assertEqual(labelled, [])

    def check_k1(self) -> dict:
        r = self.res("k1")
        self.assertEqual((r["first"]["complete"], r["second"]["complete"]), ("success", "success"), r)
        self.assertEqual(r["first"]["finals"], ["Ответ k1a"])
        self.assertEqual(r["second"]["finals"], ["Ответ k1b"])
        self.assertTrue(r["history_has_first"], "the second turn did not see the first one")
        keys = {x["session_key"] for x in r["sessions"].get("routing", [])}
        print(f"K1 keys={keys} sessions={r['sessions']} tools={r['tools']} hint={r['hint_in_prompt']}")
        return r


class GroupA(_Group):
    GROUP = "A"
    SCENARIOS = SCENARIOS_A

    def test_K1_one_session_with_history(self) -> None:
        self.check_k1()

    def test_K3_new_chat_is_a_new_session(self) -> None:
        r = self.res("k3")
        self.assertEqual(r["turn"]["finals"], ["Ответ k3"])
        self.assertFalse(r["history_has_k1"], "a new chat id saw another conversation")
        print(f"K3 sessions={r['sessions']}")

    def test_Z1_compression(self) -> None:
        r = self.res("z1")
        print(f"Z1 {json.dumps(r, ensure_ascii=False, default=str)[:5000]}")

    def test_Q1_tools_without_clarify(self) -> None:
        tools = self.res("k1")["tools"]
        print(f"Q1 tools={tools}")

    def test_Q2_question_back_is_an_ordinary_final(self) -> None:
        r = self.res("q2")
        self.assertEqual(r["turn"]["complete"], "success")
        self.assertEqual(r["turn"]["finals"], ["Какой жанр вам нравится?"])
        self.assertEqual(r["clarify"], [])

    def test_Q3_clarify_called_anyway(self) -> None:
        print(f"Q3 {json.dumps(self.res('q3'), ensure_ascii=False, default=str)[:3000]}")

    def test_C_cron_wave(self) -> None:
        r = self.res("cron")
        print(f"C1-C3 {json.dumps(r, ensure_ascii=False, default=str)[:8000]}")

    def test_C2_routed_telegram_chat_gets_the_result(self) -> None:
        r = self.res("cron")
        hits = [s for s in r["tg"] if s["chat"] == str(TG_OWNER) and "Напоминание c2" in s["text"]]
        self.assertTrue(hits, r["tg"])

    def test_C4_run_from_voice(self) -> None:
        print(f"C4 {json.dumps(self.res('c4'), ensure_ascii=False, default=str)[:5000]}")

    def test_O1_out_of_turn_goes_to_telegram_labelled(self) -> None:
        r = self.res("o1")
        self.assertTrue([f for f in r["forwards"] if f.get("status") == "delivered"], r)
        self.assertTrue([s for s in r["tg"] if s["text"].startswith("Алиса:") and s["chat"] == str(TG_OWNER)], r)
        print(f"O1 {r}")

    def test_strict_negative_nothing_inside_a_turn_is_forwarded(self) -> None:
        self.assert_no_label_from(["chat-k", "chat-new", "chat-z", "chat-q2"])


class GroupB(_Group):
    GROUP = "B"
    SCENARIOS = SCENARIOS_B

    def test_K1_still_one_session_with_history(self) -> None:
        self.check_k1()

    def test_H2_hook_rewrites_only_alice(self) -> None:
        print(f"H2 {json.dumps(self.res('h2'), ensure_ascii=False, default=str)[:5000]}")

    def test_H3_voice_job_reaches_telegram(self) -> None:
        r = self.res("h3")
        self.assertEqual((r["jobs"].get("h3") or {}).get("deliver"), TARGET, r["jobs"])
        hits = [s for s in r["tg"] if s["chat"] == str(TG_OWNER) and "Напоминание h3" in s["text"]]
        self.assertTrue(hits, r)

    def test_H5_telegram_turn_of_the_target_profile_untouched(self) -> None:
        r = self.res("h5")
        self.assertIsNotNone(r["reply"])
        self.assertTrue(all(not h["merged"] for h in r["hooks"]), r["hooks"])
        self.assertNotEqual((r["job"] or {}).get("deliver"), TARGET, r["job"])
        print(f"H5 {r}")

    def test_strict_negative_nothing_inside_a_turn_is_forwarded(self) -> None:
        self.assert_no_label_from(["chat-k"])


class GroupC(_Group):
    GROUP = "C"
    SCENARIOS = SCENARIOS_C

    def test_H4_what_the_hook_sees_without_the_chat_in_p2(self) -> None:
        print(f"H4 {json.dumps(self.res('h4'), ensure_ascii=False, default=str)[:3000]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
