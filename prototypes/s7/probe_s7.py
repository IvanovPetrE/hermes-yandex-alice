"""Prototype S7: ``hermes_state`` as the source of "repeat" (YANDEX.REPEAT) for the voice conversation.

What it answers (``docs/PLAN.md``, stage 1, S7): can the stage-3 adapter, running inside the gateway process (the
default profile's scope), through the public plugin API only, find the voice conversation of the TARGET profile by
``chat_id`` and read the last agent answer with text - after a plain turn, a tool turn, compression and a gateway
restart - within the ~3.5 s budget?

How: the gateway is a CHILD process (``probe_s7.py child``): a real ``GatewayRunner`` (multiplex default + ``p2``,
route ``yandex_alice -> p2``; group C without a route) with the probe plugin; the probe process holds the scripted
loopback LLM (``tests/fakes/fake_llm_provider``) and sends JSON-line commands (``inject``, ``recall``, ``bench``,
``storm``, ``stats``, ``stop``). A "restart" is a new child in the SAME throwaway home (graceful stop or SIGKILL).
The probe reads the target ``state.db`` itself (``mode=ro``) only to cross-check what the plugin read.

PROBE ONLY. ``gateway.run``, ``tests.*`` and the runner's private attributes are the harness, NOT the plugin API; the
plugin (``plugin/``) imports only ``public-api.md`` modules and never reads ``os.environ``. Run through ``run.sh``.
Pure-layer tests need no Hermes: ``cd prototypes/s7 && python -m unittest probe_s7 -k HistoryTests``. Python >= 3.11.

Hard assertions only where the criterion is binary (A0, A1, A2, A5, A9, B1, C1); the rest print facts that the
verdict in ``docs/RECON.md`` reads.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
PLATFORM = "yandex_alice"
PLUGIN_NAME = "yandex-alice-s7"
P2 = "p2"
OWNER_ID = "u-probe-owner"  # synthetic
SECRET = "s7-synthetic-path-secret-0123456789"  # synthetic
TOOLS_EAGER = "tools:\n  tool_search:\n    enabled: 'off'\n"
BIG_TOKENS = 125_000  # > 0.85 x the 128k window the fake model reports: forces compression
START_TIMEOUT = 150.0
TURN_TIMEOUT = 90.0
TODO_ARGS = {"todos": [{"id": "1", "content": "купить хлеб", "status": "pending"}]}


def _load_history():
    spec = importlib.util.spec_from_file_location("s7_history", HERE / "plugin" / "history.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- the pure layer, no Hermes ---------------------------------------------------------------------


class FakeDb:
    """Just ``get_messages(latest=True, limit, offset)`` over a list, oldest first."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def get_messages(self, session_id, latest=False, limit=None, offset=0, **kw):
        self.calls += 1
        assert latest
        newest_first = self.rows[::-1][offset:][:limit]
        return newest_first[::-1]


class Route:
    def __init__(self, platform, profile, enabled=True, **kw):
        self.platform, self.profile, self.enabled = platform, profile, enabled
        for name in ("chat_id", "thread_id", "guild_id", "user_id"):
            setattr(self, name, kw.get(name))


def asst(text=None, tools=False, summary=False):
    row = dict(role="assistant", content=text, active=1)
    if tools:
        row["tool_calls"] = [{"id": "c1"}]
    if summary:
        row["_compressed_summary"] = True
    return row


class HistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.h = _load_history()

    def test_newest_assistant_text_wins(self) -> None:
        rows = [asst("старый"), dict(role="user", content="вопрос"), asst("новый")]
        self.assertEqual(self.h.last_answer(rows), "новый")

    def test_comment_next_to_a_tool_call_is_not_the_answer(self) -> None:
        rows = [asst("Итог."), dict(role="user", content="q"), asst("Сейчас запишу.", tools=True),
                dict(role="tool", content="ok")]
        self.assertEqual(self.h.last_answer(rows), "Итог.")

    def test_summary_empty_and_non_text_are_skipped(self) -> None:
        rows = [asst("Итог."), asst("Сводка сжатия", summary=True), asst(""), asst("   "), asst(None),
                dict(role="assistant", content=[{"type": "text", "text": "части"}])]
        self.assertEqual(self.h.last_answer(rows), "Итог.")

    def test_user_and_tool_rows_are_never_answers(self) -> None:
        self.assertIsNone(self.h.last_answer([dict(role="user", content="q"), dict(role="tool", content="r")]))
        self.assertIsNone(self.h.last_answer([]))
        self.assertIsNone(self.h.last_answer(None))

    def test_tail_is_read_in_portions_until_an_answer(self) -> None:
        rows = [asst("Давний ответ")] + [asst(None, tools=True), dict(role="tool", content="r")] * 3
        db = FakeDb(rows)
        self.assertEqual(self.h.read_tail(db, "s", tail=2, portions=5), ("Давний ответ", 4))

    def test_portions_are_capped(self) -> None:
        rows = [asst("Давний ответ")] + [asst(None, tools=True), dict(role="tool", content="r")] * 6
        db = FakeDb(rows)
        self.assertEqual(self.h.read_tail(db, "s", tail=2, portions=3), (None, 3))
        self.assertEqual(db.calls, 3)

    def test_short_history_stops_early(self) -> None:
        db = FakeDb([dict(role="user", content="q")])
        self.assertEqual(self.h.read_tail(db, "s", tail=20, portions=5), (None, 1))

    def test_target_profile_comes_from_the_plain_route(self) -> None:
        routes = [Route("telegram", "tg"), Route(PLATFORM, "off", enabled=False), Route(PLATFORM, "narrow", user_id="u"),
                  Route(PLATFORM, "target")]
        self.assertEqual(self.h.pick_profile(routes, PLATFORM), "target")
        self.assertEqual(self.h.pick_profile([], PLATFORM), "default")
        self.assertEqual(self.h.pick_profile(None, PLATFORM), "default")

    def test_a_second_read_is_refused_while_one_is_running(self) -> None:
        self.assertTrue(self.h._LOCK.acquire(blocking=False))
        try:
            out = self.h.read_last_answer(PLATFORM, "chat", db=object())
        finally:
            self.h._LOCK.release()
        self.assertEqual((out["outcome"], out["why"]), ("error", "busy"))

    def test_steps_are_filled_while_the_worker_runs_and_returned(self) -> None:
        class NoSession:
            def find_session_by_origin(self, **kw):
                return None

        trace: dict = {}
        out = self.h.read_last_answer(PLATFORM, "chat", db=NoSession(), steps=trace)
        self.assertEqual(out["outcome"], "missing")
        self.assertIs(out["steps"], trace)
        self.assertIn("find", trace)

    def test_a_given_path_skips_the_routes_step(self) -> None:
        out = self.h.read_last_answer(PLATFORM, "chat", db_path=Path("/nonexistent/state.db"))
        self.assertEqual((out["outcome"], out["why"]), ("error", "no_state_db"))
        self.assertNotIn("profile", out)

    def test_a_failing_db_is_an_error_outcome_and_frees_the_lock(self) -> None:
        class Boom:
            def find_session_by_origin(self, **kw):
                raise RuntimeError("locked")

        out = self.h.read_last_answer(PLATFORM, "chat", db=Boom())
        self.assertEqual((out["outcome"], out["why"]), ("error", "RuntimeError"))
        self.assertTrue(self.h._LOCK.acquire(blocking=False))
        self.h._LOCK.release()


# --- harness (Hermes) ------------------------------------------------------------------------------

LIVE: set = set()
RSS_PEAK = {"mb": 0, "self": 0}


def _vmrss_mb(pid: Any) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def _start_watchdog() -> None:
    """Total RSS of the probe and every live child gateway; abort the lot above the limit."""
    if RSS_PEAK.get("armed"):
        return
    RSS_PEAK["armed"] = 1
    limit = int(os.environ.get("S7_RSS_LIMIT_MB") or 0)

    def loop() -> None:
        while True:
            own = _vmrss_mb("self")
            total = own + sum(_vmrss_mb(c.pid) for c in list(LIVE))
            RSS_PEAK["mb"] = max(RSS_PEAK["mb"], total)
            RSS_PEAK["self"] = max(RSS_PEAK["self"], own)
            if limit and total > limit:
                print(f"\n!!! total RSS {total} MB > limit {limit} MB - probe aborted by the watchdog", flush=True)
                for child in list(LIVE):
                    try:
                        child.proc.kill()
                    except OSError:
                        pass
                os._exit(137)
            time.sleep(0.5)

    threading.Thread(target=loop, name="s7-rss-watchdog", daemon=True).start()


def _guard_temp_home() -> Path:
    import pwd  # POSIX only

    raw = os.environ.get("HERMES_HOME", "").strip()
    if not raw:
        raise SystemExit("refusing: HERMES_HOME is not set (run through run.sh)")
    home = Path(raw).resolve()
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if real_home == home or real_home in home.parents:
        raise SystemExit(f"refusing: HERMES_HOME {home} is inside the real home of this user")
    if not any(p.name.startswith("hya-s7-") for p in home.parents):
        raise SystemExit(f"refusing: HERMES_HOME {home} is not under a hya-s7-* temp dir")
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


def _trailing_tools(body: dict) -> int:
    n = 0
    for msg in reversed(body.get("messages") or []):
        if msg.get("role") == "tool":
            n += 1
        elif msg.get("role") == "user":
            break
    return n


def _all_text(body: dict) -> str:
    return "\n".join(_text_of(m.get("content")) for m in body.get("messages") or [])


class Llm:
    """Scripted loopback LLM, keyed by a ``[token]`` in the latest user message."""

    def __init__(self) -> None:
        from tests.fakes import fake_llm_provider as f

        self.f = f
        self.big = False
        self.hold = threading.Event()
        self.holding = threading.Event()
        self.srv = f.FakeLLMServer(self, prompt_tokens_fn=self._tokens)
        self.srv.start()
        self.base_url = self.srv.base_url

    def _tokens(self, body: dict) -> int:
        return BIG_TOKENS if self.big and "[z" in _all_text(body) else 100

    def __call__(self, record: dict):
        f = self.f
        body = record.get("body") or {}
        user = _last_user_text(body)
        tools = _trailing_tools(body)
        if "[System note: The previous turn was interrupted" in user:  # Hermes' auto-resume turn after a restart
            return f.Text("Ответ после перезапуска")
        m = re.search(r"\[([a-z0-9]+)\]", user)
        token = m.group(1) if m else ""
        if token == "hold":
            if tools < 2:
                return f.ToolCall("todo_list", TODO_ARGS, text="Сейчас записываю.")
            self.holding.set()
            self.hold.wait(300)
            return f.Text("Ответ после удержания")
        if token == "a2":
            if tools == 0:
                return f.ToolCall("todo_list", TODO_ARGS, text="Сейчас запишу.")
            return f.Text("Итог a2: записал.")
        if token == "a3":
            return f.Text("ГОЛОС: Коротко. ТЕЛЕГРАМ: Подробности https://example.test")
        if token == "a4":
            return f.Text("")
        if token == "quick":
            return f.Text("Быстрый ответ")
        if token == "zafter":
            return f.Text("Ответ после сжатия")
        if token.startswith("z"):  # real bulk, not only reported tokens: else Hermes refuses a summary longer than the history
            return f.Text(f"Ответ {token}. " + "Подробности беседы. " * 300)
        return f.Text(f"Ответ {token}")

    def mains(self) -> list:
        return [r for r in list(self.srv.requests) if r.get("kind") == "main"]

    def next_phase(self) -> None:
        self.hold.set()  # free the threads of the dead run; the next run gets a fresh event
        self.hold = threading.Event()
        self.holding = threading.Event()

    def stop(self) -> None:
        self.hold.set()
        self.srv.stop()


def _insert_into_agent(cfg: Path, lines: str) -> None:
    text = cfg.read_text(encoding="utf-8")
    if text.count("\nagent:\n") != 1:
        raise AssertionError(f"expected exactly one agent: section in {cfg}")
    cfg.write_text(text.replace("\nagent:\n", "\nagent:\n" + lines, 1), encoding="utf-8")


class Stand:
    """One throwaway gateway home: default profile (+ ``p2`` when routed), the plugin, the journal."""

    def __init__(self, root: Path, name: str, llm: Llm, *, route: bool = True) -> None:
        self.name, self.llm, self.route = name, llm, route
        self.base = root / name
        self.home = self.base / "home" / ".hermes"
        self.target = self.home / "profiles" / P2 if route else self.home
        self.journal = self.home / "s7-journal.jsonl"
        self.control = self.home / "s7-control"

    def write(self) -> None:
        from tests.fakes import fake_llm_provider as f

        for sub in ("locks", "tmp"):
            (self.base / sub).mkdir(parents=True, exist_ok=True)
        compression = "compression:\n  protect_last_n: 2\n"
        toolsets = f"platform_toolsets:\n  {PLATFORM}: [todo]\n"
        routing = ""
        if self.route:
            routing = ("gateway:\n  multiplex_profiles: true\n  loop_watchdog: false\n  profile_routes:\n"
                       f"    - name: alice-probe\n      platform: {PLATFORM}\n      profile: {P2}\n")
        f.write_hermes_home(self.home, self.llm.base_url, extra_config=(
            routing + f"plugins:\n  enabled:\n    - {PLUGIN_NAME}\n"
            f"platforms:\n  {PLATFORM}:\n    enabled: true\n    gateway_restart_notification: false\n"
            "    extra:\n      interactive_resume: false\n"
            + ("" if self.route else toolsets + compression) + TOOLS_EAGER))
        homes = [self.home]
        if self.route:
            f.write_hermes_home(self.target, self.llm.base_url, extra_config=toolsets + compression + TOOLS_EAGER)
            homes.append(self.target)
        for h in homes:
            _insert_into_agent(h / "config.yaml", "  auto_recovery_cycles: 0\n")
        with (self.home / ".env").open("a", encoding="utf-8") as fh:
            fh.write(f"ALICE_PATH_SECRET={SECRET}\nALICE_OWNER_USER_ID={OWNER_ID}\n")
        shutil.copytree(HERE / "plugin", self.home / "plugins" / PLUGIN_NAME,
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.control.mkdir(exist_ok=True)

    def env(self) -> dict:
        env = dict(os.environ)
        env.update(HOME=str(self.base / "home"), HERMES_HOME=str(self.home),
                   HERMES_GATEWAY_LOCK_DIR=str(self.base / "locks"), TMPDIR=str(self.base / "tmp"))
        return env

    def rows(self) -> list:
        out = []
        try:
            for line in self.journal.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        except OSError:
            pass
        return out

    def wait_row(self, pred, timeout: float, start: int = 0) -> Optional[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for row in self.rows()[start:]:
                if pred(row):
                    return row
            time.sleep(0.1)
        return None

    def db_view(self, chat: str) -> dict:
        """What the TARGET profile's ``state.db`` holds for the conversation ``chat`` (cross-check, read-only)."""
        path = self.target / "state.db"
        if not path.exists():
            return {"state_db": "missing"}
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(sessions)")]
            want = [c for c in ("id", "parent_session_id", "source", "chat_id", "session_key", "ended_at", "end_reason",
                                "message_count", "profile_name") if c in cols]
            sessions = [dict(r) for r in con.execute(
                f"SELECT {', '.join(want)} FROM sessions WHERE chat_id = ? OR session_key LIKE ?", (chat, "%:" + chat))]
            mcols = [r[1] for r in con.execute("PRAGMA table_info(messages)")]
            for s in sessions:
                rows = con.execute("SELECT * FROM messages WHERE session_id = ? ORDER BY id", (s["id"],)).fetchall()
                s["messages"] = len(rows)
                s["tail"] = [dict(role=r["role"], tool_calls=bool(r["tool_calls"]) if "tool_calls" in mcols else None,
                                  length=len(str(r["content"] or "")),
                                  active=r["active"] if "active" in mcols else None,
                                  summary=bool(r["_compressed_summary"]) if "_compressed_summary" in mcols else None,
                                  head=str(r["content"] or "")[:40]) for r in rows[-6:]]
            return dict(sessions=sessions)
        finally:
            con.close()


class Child:
    """The gateway as a child process; JSON-line commands on stdin, ``S7 {...}`` events on stdout."""

    def __init__(self, stand: Stand) -> None:
        self.stand = stand
        self.events: list = []
        self.lines: list = []
        self._cv = threading.Condition()
        self.eof = False
        self._next = 0
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-u", str(HERE / "probe_s7.py"), "child"], env=stand.env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            cwd=str(stand.base))
        self.pid = self.proc.pid
        self.t_start = time.monotonic()
        LIVE.add(self)
        threading.Thread(target=self._read, name="s7-child-reader", daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("S7 "):
                try:
                    event = json.loads(line[3:])
                except ValueError:
                    event = None
                if event is not None:
                    with self._cv:
                        self.events.append(event)
                        self._cv.notify_all()
                    continue
            self.lines.append(line)
        with self._cv:
            self.eof = True
            self._cv.notify_all()

    def wait_event(self, name: str, timeout: float, match=None) -> Optional[dict]:
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                for event in self.events:
                    if event.get("ev") == name and (match is None or match(event)):
                        return event
                left = deadline - time.monotonic()
                if left <= 0 or self.eof:
                    return None
                self._cv.wait(min(left, 0.5))

    def cmd(self, op: str, timeout: float = 60.0, **kw: Any) -> Optional[dict]:
        self._next += 1
        ident = self._next
        try:
            self.proc.stdin.write(json.dumps(dict(op=op, id=ident, **kw)) + "\n")
            self.proc.stdin.flush()
        except OSError:
            return None
        return self.wait_event("resp", timeout, lambda e: e.get("id") == ident)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def wait_exit(self, timeout: float) -> bool:
        try:
            self.proc.wait(timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def kill(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass
        self.wait_exit(15)
        LIVE.discard(self)

    def close(self) -> None:
        if self.alive():
            self.kill()
        LIVE.discard(self)


def start_gateway(stand: Stand) -> tuple:
    child = Child(stand)
    return child, child.wait_event("started", START_TIMEOUT)


def stop_gracefully(child: Child, timeout: float = 90.0) -> dict:
    t0 = time.monotonic()
    resp = child.cmd("stop", timeout)
    exited = child.wait_exit(20) if resp else False
    forced = False
    if not exited:
        child.kill()
        forced = True
    return dict(answered=bool(resp), took=round(time.monotonic() - t0, 2), forced=forced)


class Ctx:
    """The running gateway of a group: turns, recalls and the cross-check view."""

    def __init__(self, stand: Stand, child: Child, llm: Llm) -> None:
        self.stand, self.child, self.llm = stand, child, llm

    def turn(self, chat: str, mid: str, token: str, text: str = "вопрос", timeout: float = TURN_TIMEOUT) -> dict:
        start = len(self.stand.rows())
        self.child.cmd("inject", text=f"[{token}] {text}", mid=mid, chat=chat)
        done = self.stand.wait_row(lambda r: r["op"] == "complete" and r.get("mid") == mid, timeout, start)
        time.sleep(0.5)
        rows = self.stand.rows()[start:]
        started = next((r for r in rows if r["op"] == "start" and r.get("mid") == mid), {})
        sends = [r for r in rows if r["op"] == "send" and r["chat"] == chat]
        return dict(complete=done["outcome"] if done else None, profile=started.get("profile"),
                    scope_home=started.get("scope_home"),
                    sends=[dict(kind=r["kind"], reply_to=r["reply_to"], notify=r["notify"], marker=r["marker"],
                                head=r["head"]) for r in sends],
                    finals=[r["head"] for r in sends if r["kind"] == "final" and r["reply_to"] == mid])

    def recall(self, chat: str, deadline: float = 1.5, **kw: Any) -> dict:
        resp = self.child.cmd("recall", 30, chat=chat, deadline=deadline, **kw)
        return resp or dict(ok=False, err="no response")

    def view(self, chat: str) -> dict:
        return self.stand.db_view(chat)


def _pct(values: list, q: float) -> float:
    ordered = sorted(values)
    return ordered[int(q * (len(ordered) - 1))] if ordered else 0.0


# --- group A: one gateway, the target profile p2 ------------------------------------------------------------------


def sc_a0(c: Ctx) -> dict:
    return dict(recall=c.recall("chat-none"), view=c.view("chat-none"))


def sc_a1(c: Ctx) -> dict:
    turn = c.turn("chat-a", "m-a1", "a1")
    return dict(turn=turn, recall=c.recall("chat-a"), view=c.view("chat-a"))


def sc_a2(c: Ctx) -> dict:
    turn = c.turn("chat-a2", "m-a2", "a2")
    return dict(turn=turn, recall=c.recall("chat-a2"), view=c.view("chat-a2"))


def sc_a3(c: Ctx) -> dict:
    turn = c.turn("chat-a3", "m-a3", "a3")
    return dict(turn=turn, recall=c.recall("chat-a3"))


def sc_a4(c: Ctx) -> dict:
    """An empty model answer: what Hermes sends as the final and what the history keeps."""
    only = c.turn("chat-a4", "m-a4", "a4")
    after_only = c.recall("chat-a4")
    c.turn("chat-a4b", "m-a4b1", "quick")
    bad = c.turn("chat-a4b", "m-a4b2", "a4")
    return dict(only=only, only_recall=after_only, only_view=c.view("chat-a4"), after_good_then_bad=bad,
                recall_after_good_then_bad=c.recall("chat-a4b"), view_b=c.view("chat-a4b"))


def sc_a5(c: Ctx) -> dict:
    c.turn("chat-a5", "m-a5", "a1")
    return dict(old=c.recall("chat-a5"), new_chat=c.recall("chat-a5-new"))


def sc_a6(c: Ctx) -> dict:
    steps = []
    c.llm.big = True
    try:
        for i in range(1, 7):
            token = f"z{i}"
            turn = c.turn("chat-z", f"m-{token}", token)
            view = c.view("chat-z")
            steps.append(dict(token=token, complete=turn["complete"], finals=[f[:30] for f in turn["finals"]],
                              sessions=[{k: s.get(k) for k in ("id", "parent_session_id", "message_count", "messages",
                                                               "ended_at", "end_reason")} for s in
                                        view.get("sessions", [])]))
            if any(s.get("parent_session_id") for s in view.get("sessions", [])) or any(
                    (s.get("messages") or 0) < 2 * i for s in view.get("sessions", [])):
                break
    finally:
        c.llm.big = False
    before = c.recall("chat-z")
    after_turn = c.turn("chat-z", "m-zafter", "zafter")
    return dict(steps=steps, recall_before_next=before, after=after_turn, recall_after=c.recall("chat-z"),
                view=c.view("chat-z"))


def sc_a7(c: Ctx) -> dict:
    """cold_uncached: routes -> profile -> path on every read; cold_cached: the path resolved in connect();
    warm: one open handle (not a stage-3 shape, the floor)."""
    uncached = c.child.cmd("bench", 120, chat="chat-a", n=20, warm=False, cached=False)
    cached = c.child.cmd("bench", 120, chat="chat-a", n=20, warm=False, cached=True)
    warm = c.child.cmd("bench", 120, chat="chat-a", n=20, warm=True)
    connect = next((r for r in c.stand.rows() if r["op"] == "connect"), {})
    return dict(cold_uncached=uncached, cold_cached=cached, warm=warm,
                connect=dict(resolved=connect.get("resolved"), profile=connect.get("profile"),
                             resolve_ms=connect.get("resolve_ms")))


def sc_a8(c: Ctx) -> dict:
    """Reads while a turn is running (tool calls done, the model call parked) and while a writer holds the db."""
    c.turn("chat-a8", "m-a8a", "a8a")
    n_before = len(c.llm.mains())
    start = len(c.stand.rows())
    c.child.cmd("inject", text="[hold] долгий вопрос", mid="m-a8b", chat="chat-a8")
    parked = c.llm.holding.wait(60)
    out: dict = dict(parked=parked, llm_calls=len(c.llm.mains()) - n_before)
    out["during_turn"] = c.recall("chat-a8")
    out["during_turn_tail1"] = c.recall("chat-a8", tail=1, portions=5)
    out["during_turn_view"] = c.view("chat-a8")
    out["bench_during_turn"] = c.child.cmd("bench", 120, chat="chat-a8", n=10, warm=False)
    # a writer holds the db for ~1 s: a read-only reader must not wait for it (WAL) or must fail fast
    writer = sqlite3.connect(str(c.stand.target / "state.db"), timeout=5, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        out["writer_locked"] = True
        out["during_write_lock"] = c.recall("chat-a8", deadline=1.5)
        out["journal_mode"] = writer.execute("PRAGMA journal_mode").fetchone()[0]
        writer.execute("ROLLBACK")
    except sqlite3.Error as exc:
        out["writer_locked"] = f"{type(exc).__name__}: {exc}"
    finally:
        writer.close()
    c.llm.hold.set()
    done = c.stand.wait_row(lambda r: r["op"] == "complete" and r.get("mid") == "m-a8b", TURN_TIMEOUT, start)
    out["turn_complete"] = done["outcome"] if done else None
    time.sleep(0.5)
    out["after_turn"] = c.recall("chat-a8")
    return out


def sc_a9(c: Ctx) -> dict:
    """Failures: wrong profile, a 1 ms deadline (sequential and concurrent storms); threads and fds before/after."""
    out: dict = dict(before=c.child.cmd("stats", 20))
    out["wrong_profile"] = c.recall("chat-a", force_profile="nope")
    out["tight_deadline"] = c.recall("chat-a", deadline=0.001)
    out["storm"] = c.child.cmd("storm", 120, chat="chat-a", n=20, deadline=0.001)
    time.sleep(5)
    out["after"] = c.child.cmd("stats", 20)
    out["normal_after"] = c.recall("chat-a")
    return out


SCENES_A = [("a0", sc_a0), ("a1", sc_a1), ("a2", sc_a2), ("a3", sc_a3), ("a4", sc_a4), ("a5", sc_a5), ("a6", sc_a6),
            ("a7", sc_a7), ("a8", sc_a8), ("a9", sc_a9)]


def run_scenes(root: Path, llm: Llm, name: str, scenes: list, *, route: bool) -> dict:
    stand = Stand(root, name, llm, route=route)
    stand.write()
    child, started = start_gateway(stand)
    res: dict = dict(started=started, rss_after_start=dict(RSS_PEAK))
    ctx = Ctx(stand, child, llm)
    try:
        for scene, fn in scenes:
            try:
                res[scene] = fn(ctx)
            except Exception as exc:  # noqa: BLE001 - recorded, asserted by the test of that scene
                res[scene] = dict(error=f"{type(exc).__name__}: {exc}")
        res["stats"] = child.cmd("stats", 20)
        res["stop"] = stop_gracefully(child)
    finally:
        child.close()
    res["rss"] = dict(RSS_PEAK)
    return res


# --- group C: voice in the default profile, no route -----------------------------------------------------------------


def sc_c1(c: Ctx) -> dict:
    turn = c.turn("chat-c1", "m-c1", "a1")
    return dict(turn=turn, recall=c.recall("chat-c1"), new_chat=c.recall("chat-c1-new"), view=c.view("chat-c1"))


SCENES_C = [("c1", sc_c1)]


# --- group B: restarts -----------------------------------------------------------------------------------------------


def _observe(stand: Stand, child: Child, mark: int, timeout: float = 60.0) -> list:
    """Rows the new run writes after boot, until the first visible outcome (+ a settle window)."""
    def mine(rows):
        return [r for r in rows[mark:] if r["pid"] == child.pid]

    deadline = time.monotonic() + timeout
    first = None
    while time.monotonic() < deadline and first is None:
        first = next((r for r in mine(stand.rows()) if r["op"] in ("complete", "send")), None)
        if first is None:
            time.sleep(0.2)
    quiet_since, last_n = time.monotonic(), -1
    while time.monotonic() < deadline + 15:
        n = len(mine(stand.rows()))
        if n != last_n:
            last_n, quiet_since = n, time.monotonic()
        if time.monotonic() - quiet_since > 5:
            break
        time.sleep(0.3)
    return mine(stand.rows())


def _compact(rows: list) -> list:
    return [{k: v for k, v in r.items() if k not in ("pid", "t", "scope_home")} for r in rows]


def restart_b1(root: Path, llm: Llm) -> dict:
    """A graceful restart in an idle moment: is the conversation found before the first turn, is it the same session."""
    stand = Stand(root, "b1", llm)
    stand.write()
    chat = "chat-b1"
    res: dict = {}
    c1, started1 = start_gateway(stand)
    res["started1"] = bool(started1 and started1.get("ok"))
    ctx = Ctx(stand, c1, llm)
    res["turn1"] = ctx.turn(chat, "m-b1", "b1")
    res["recall1"] = ctx.recall(chat)
    res["view1"] = ctx.view(chat)
    res["stop1"] = stop_gracefully(c1)
    c2, started2 = start_gateway(stand)
    res["started2"] = bool(started2 and started2.get("ok"))
    ctx = Ctx(stand, c2, llm)
    res["recall2_before_any_turn"] = ctx.recall(chat)
    res["view2"] = ctx.view(chat)
    res["turn2"] = ctx.turn(chat, "m-b1b", "b1b")
    res["recall3"] = ctx.recall(chat)
    res["view3"] = ctx.view(chat)
    res["stop2"] = stop_gracefully(c2)
    c1.close()
    c2.close()
    return res


def restart_b2(root: Path, llm: Llm) -> dict:
    """A graceful stop in the middle of a turn: Hermes continues it after the start (an internal turn)."""
    stand = Stand(root, "b2", llm)
    stand.write()
    chat = "chat-b2"
    res: dict = {}
    c1, started1 = start_gateway(stand)
    res["started1"] = bool(started1 and started1.get("ok"))
    ctx = Ctx(stand, c1, llm)
    ctx.turn(chat, "m-b2a", "b2a")
    res["recall_before"] = ctx.recall(chat)
    c1.cmd("inject", text="[hold] долгий вопрос", mid="m-b2b", chat=chat)
    res["parked"] = llm.holding.wait(60)
    res["recall_during_turn"] = ctx.recall(chat)
    res["stop1"] = stop_gracefully(c1)
    llm.next_phase()
    mark = len(stand.rows())
    c2, started2 = start_gateway(stand)
    res["started2"] = bool(started2 and started2.get("ok"))
    ctx = Ctx(stand, c2, llm)
    res["recall_right_after_start"] = ctx.recall(chat)
    rows2 = _observe(stand, c2, mark)
    res["rows2"] = _compact(rows2)
    res["recall_after_resume"] = ctx.recall(chat)
    res["view"] = ctx.view(chat)
    res["stop2"] = stop_gracefully(c2)
    c1.close()
    c2.close()
    return res


def restart_b3(root: Path, llm: Llm) -> dict:
    """SIGKILL inside the final send(): the ledger redelivers the text (marked) outside a turn after the start."""
    stand = Stand(root, "b3", llm)
    stand.write()
    chat = "chat-b3"
    res: dict = {}
    c1, started1 = start_gateway(stand)
    res["started1"] = bool(started1 and started1.get("ok"))
    ctx = Ctx(stand, c1, llm)
    ctx.turn(chat, "m-b3a", "b3a")
    (stand.control / "hold_final").write_text("1", encoding="utf-8")
    c1.cmd("inject", text="[quick] вопрос", mid="m-b3b", chat=chat)
    res["held"] = stand.wait_row(lambda r: r["op"] == "held", 60) is not None
    res["recall_while_final_is_held"] = ctx.recall(chat)
    res["view_before_kill"] = ctx.view(chat)
    c1.kill()
    (stand.control / "hold_final").unlink(missing_ok=True)
    llm.next_phase()
    mark = len(stand.rows())
    c2, started2 = start_gateway(stand)
    res["started2"] = bool(started2 and started2.get("ok"))
    ctx = Ctx(stand, c2, llm)
    rows2 = _observe(stand, c2, mark)
    res["rows2"] = _compact(rows2)
    res["stats_before_recall"] = c2.cmd("stats", 20)
    res["recall_after_ledger"] = ctx.recall(chat)   # the first read of the new process, 1.5 s deadline
    res["recall_after_ledger_2"] = ctx.recall(chat, deadline=5.0)
    res["recall_after_ledger_3"] = ctx.recall(chat, deadline=5.0, use_cache=False)
    res["stats_after_recall"] = c2.cmd("stats", 20)
    res["view_after"] = ctx.view(chat)
    res["stop2"] = stop_gracefully(c2)
    c1.close()
    c2.close()
    return res


SCENES_B = [("b1", restart_b1), ("b2", restart_b2), ("b3", restart_b3)]


def run_restarts(root: Path, llm: Llm) -> dict:
    out = {}
    for name, fn in SCENES_B:
        try:
            out[name] = fn(root, llm)
        except Exception as exc:  # noqa: BLE001 - recorded, asserted by the test of that scene
            out[name] = dict(error=f"{type(exc).__name__}: {exc}")
    out["rss"] = dict(RSS_PEAK)
    return out


# --- the child: the gateway ------------------------------------------------------------------------------------------


def emit(**kw: Any) -> None:
    print("S7 " + json.dumps(kw, ensure_ascii=False, default=str), flush=True)


def _stats() -> dict:
    try:
        fds = len(os.listdir("/proc/self/fd"))
    except OSError:
        fds = -1
    try:
        load1 = float(Path("/proc/loadavg").read_text().split()[0])
        mem = next(int(line.split()[1]) // 1024 for line in Path("/proc/meminfo").read_text().splitlines()
                   if line.startswith("MemAvailable:"))
    except (OSError, ValueError, StopIteration):
        load1, mem = -1.0, -1
    return dict(threads=threading.active_count(), fds=fds, rss_mb=_vmrss_mb("self"), load1=load1, mem_available_mb=mem)


async def _child_main() -> None:
    import asyncio
    from collections import Counter

    from gateway.config import Platform, load_gateway_config
    from gateway.run import GatewayRunner
    from hermes_logging import setup_logging

    setup_logging(mode="gateway")
    history = _load_history()
    runner = GatewayRunner(load_gateway_config())
    ok = await runner.start()
    loop = asyncio.get_running_loop()
    tasks: set = set()

    def alice():
        return runner.adapters.get(Platform(PLATFORM))

    def name_of(key) -> str:
        return str(getattr(key, "value", key))

    emit(ev="started", ok=bool(ok), alice=alice() is not None, adapters=sorted(name_of(k) for k in runner.adapters))

    async def lag_ticker(stop: list, lags: list) -> None:
        while not stop:
            t = time.perf_counter()
            await asyncio.sleep(0.005)
            lags.append((time.perf_counter() - t - 0.005) * 1000)

    async def handle(cmd: dict) -> dict:
        op = cmd["op"]
        a = alice()
        if op == "stats":
            return _stats()
        if op == "inject":  # a turn runs on; the harness waits for the journal
            task = asyncio.create_task(a.probe_inject(cmd["text"], cmd["mid"], cmd["chat"]))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            return {}
        if op == "recall":
            kw = {k: cmd[k] for k in ("tail", "portions", "force_profile", "use_cache") if k in cmd}
            return await a.probe_recall(cmd["chat"], float(cmd.get("deadline", 1.5)), **kw)
        if op == "bench":
            stop: list = []
            lags: list = []
            ticker = asyncio.create_task(lag_ticker(stop, lags))
            times: list = []
            outcomes: Counter = Counter()
            db = None
            try:
                if cmd.get("warm"):
                    from hermes_state import SessionDB

                    profile = history.pick_profile(load_gateway_config().profile_routes, PLATFORM)
                    db = SessionDB(history.target_home(profile) / "state.db", read_only=True)
                for _ in range(int(cmd["n"])):
                    if db is not None:
                        t = time.perf_counter()
                        r = await asyncio.to_thread(history.read_last_answer, PLATFORM, cmd["chat"], db=db)
                        ms = (time.perf_counter() - t) * 1000
                    else:
                        r = await a.probe_recall(cmd["chat"], 5.0, use_cache=bool(cmd.get("cached", True)))
                        ms = r["took_ms"]
                    times.append(ms)
                    outcomes[r["outcome"]] += 1
            finally:
                stop.append(1)
                await ticker
                if db is not None:
                    db.close()
            return dict(n=len(times), outcomes=dict(outcomes), p50=round(_pct(times, 0.5), 1),
                        p95=round(_pct(times, 0.95), 1), max=round(max(times or [0]), 1),
                        loop_lag_max_ms=round(max(lags or [0]), 1), loop_lag_p95_ms=round(_pct(lags, 0.95), 1))
        if op == "storm":
            n, deadline = int(cmd["n"]), float(cmd["deadline"])
            seq = [await a.probe_recall(cmd["chat"], deadline) for _ in range(n)]
            conc = await asyncio.gather(*[a.probe_recall(cmd["chat"], deadline) for _ in range(n)])
            await asyncio.sleep(4)
            def tally(results: list) -> dict:   # JSON keys are strings: "outcome/why"
                return dict(Counter(f"{r['outcome']}/{r.get('why')}" for r in results))

            return dict(sequential=tally(seq), concurrent=tally(conc), stats_after=_stats())
        if op == "stop":
            await runner.stop()
            return {}
        raise ValueError(f"unknown op {op}")

    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:  # the probe is gone: do not linger and eat memory
            os._exit(1)
        cmd = json.loads(line)
        try:
            out = await handle(cmd)
            emit(ev="resp", id=cmd["id"], ok=True, **{k: v for k, v in out.items() if k not in ("ok", "id")})
        except Exception as exc:  # noqa: BLE001
            emit(ev="resp", id=cmd["id"], ok=False, err=f"{type(exc).__name__}: {exc}")
        if cmd["op"] == "stop":
            os._exit(0)


def child_entry() -> None:
    import asyncio

    sys.path.insert(0, os.environ["S7_CHECKOUT"])
    sys._hermes_pin_default_home = True
    import hermes_bootstrap  # noqa: F401  (sets up the Hermes runtime site-packages)

    _guard_temp_home()
    asyncio.run(_child_main())


# --- groups under unittest -------------------------------------------------------------------------------------------


def _dump(title: str, res: dict) -> None:
    print(f"\n=== {title}")
    print(json.dumps(res, ensure_ascii=False, default=str, indent=1)[:60000])


def _validate_plugin(case: unittest.TestCase) -> None:
    """``hermes plugins validate`` on the probe plugin: imports outside public-api.md."""
    try:
        from hermes_cli.plugin_validate import validate_plugin_dir

        report = validate_plugin_dir(HERE / "plugin", None)
        v = dict(ok=bool(report.ok), warnings=[str(w) for w in report.warnings],
                 checks=[[str(n), bool(good), str(d or "")[:200]] for n, good, d in report.checks])
    except Exception as exc:  # noqa: BLE001
        v = dict(error=f"{type(exc).__name__}: {exc}")
    print(f"validate={json.dumps(v, ensure_ascii=False)}")
    case.assertNotIn("error", v, v)
    case.assertFalse([w for w in v["warnings"] if "import" in w.lower()], v["warnings"])


class _Base(unittest.TestCase):
    res: dict = {}
    llm: Llm

    @classmethod
    def _root(cls) -> Path:
        home = _guard_temp_home()
        return home.parents[1] / "scenes"  # <hya-s7-*>/root-<G>/scenes; the run.sh home is <root>/home/.hermes

    def scene(self, name: str) -> dict:
        r = self.res[name]
        self.assertNotIn("error", r, r.get("error"))
        return r

    def test_00_rss(self) -> None:
        print(f"rss_peak_total_mb={RSS_PEAK['mb']} probe_only_mb={RSS_PEAK['self']}")
        self.assertGreater(RSS_PEAK["mb"], 0)

    def test_validate(self) -> None:
        _validate_plugin(self)


class GroupA(_Base):
    @classmethod
    def setUpClass(cls) -> None:
        _start_watchdog()
        cls.llm = Llm()
        try:
            cls.res = run_scenes(cls._root(), cls.llm, "a", SCENES_A, route=True)
        finally:
            cls.llm.stop()
        _dump("group A: routed to p2", cls.res)

    def test_started(self) -> None:
        self.assertTrue(self.res["started"] and self.res["started"]["ok"], self.res["started"])

    def test_A0_unknown_conversation_is_missing(self) -> None:
        r = self.scene("a0")["recall"]
        self.assertEqual((r["outcome"], r.get("session_found")), ("missing", False), r)

    def test_A1_plain_answer_is_read_back_from_the_target_profile(self) -> None:
        r = self.scene("a1")
        self.assertEqual(r["turn"]["finals"], ["Ответ a1"], r["turn"])
        self.assertEqual((r["recall"]["outcome"], r["recall"]["text"]), ("found", "Ответ a1"), r["recall"])
        print(f"A1 turn profile={r['turn']['profile']} scope_home={r['turn']['scope_home']} "
              f"recall scope_home={r['recall']['scope_home']} chain={r['recall'].get('profile')}")

    def test_A2_tool_turn_returns_the_final_not_the_comment(self) -> None:
        r = self.scene("a2")["recall"]
        self.assertEqual((r["outcome"], r["text"]), ("found", "Итог a2: записал."), r)

    def test_A3_raw_text_with_voice_and_telegram_parts(self) -> None:
        r = self.scene("a3")["recall"]
        print(f"A3 recall={r['outcome']} text={r['text']!r}")

    def test_A4_empty_model_answer(self) -> None:
        r = self.scene("a4")
        print(f"A4 only={r['only']} only_recall={r['only_recall']} after_good_then_bad={r['after_good_then_bad']} "
              f"recall_after_good_then_bad={r['recall_after_good_then_bad']}")

    def test_A5_new_conversation_is_not_the_old_one(self) -> None:
        r = self.scene("a5")
        self.assertEqual(r["old"]["text"], "Ответ a1")
        self.assertEqual((r["new_chat"]["outcome"], r["new_chat"].get("session_found")), ("missing", False))

    def test_A6_compression(self) -> None:
        r = self.scene("a6")
        print(f"A6 recall_before_next={r['recall_before_next']} recall_after={r['recall_after']} steps={r['steps']}")

    def test_A7_time(self) -> None:
        r = self.scene("a7")
        print(f"A7 uncached={r['cold_uncached']} cached={r['cold_cached']} warm={r['warm']} connect={r['connect']}")

    def test_A8_reads_during_a_turn_and_a_write_lock(self) -> None:
        r = self.scene("a8")
        print(f"A8 {json.dumps(r, ensure_ascii=False, default=str)[:3000]}")

    def test_A9_failures_do_not_pile_up_threads_or_files(self) -> None:
        r = self.scene("a9")
        self.assertEqual((r["wrong_profile"]["outcome"], r["tight_deadline"]["outcome"]), ("error", "error"), r)
        self.assertEqual(r["normal_after"]["text"], "Ответ a1", r["normal_after"])
        before, after = r["before"], r["after"]
        print(f"A9 before={before} after={after} storm={r['storm']}")
        self.assertTrue(r["storm"] and r["storm"].get("ok"), r["storm"])   # the storm itself must have run
        self.assertLessEqual(r["storm"]["stats_after"]["threads"] - before["threads"], 3, r["storm"])
        self.assertLessEqual(r["storm"]["stats_after"]["fds"] - before["fds"], 10, r["storm"])
        self.assertLessEqual(after["threads"] - before["threads"], 3, (before, after))
        self.assertLessEqual(after["fds"] - before["fds"], 10, (before, after))


class GroupC(_Base):
    @classmethod
    def setUpClass(cls) -> None:
        _start_watchdog()
        cls.llm = Llm()
        try:
            cls.res = run_scenes(cls._root(), cls.llm, "c", SCENES_C, route=False)
        finally:
            cls.llm.stop()
        _dump("group C: voice in the default profile (no route)", cls.res)

    def test_C1_default_profile_conversation_is_found(self) -> None:
        r = self.scene("c1")
        self.assertEqual((r["recall"]["outcome"], r["recall"]["text"]), ("found", "Ответ a1"), r["recall"])
        self.assertEqual(r["recall"].get("profile"), "default")
        self.assertEqual(r["new_chat"]["outcome"], "missing")


class GroupB(_Base):
    @classmethod
    def setUpClass(cls) -> None:
        _start_watchdog()
        cls.llm = Llm()
        try:
            cls.res = run_restarts(cls._root(), cls.llm)
        finally:
            cls.llm.stop()
        _dump("group B: restarts", cls.res)

    def test_B1_idle_restart_keeps_the_conversation_findable(self) -> None:
        r = self.scene("b1")
        self.assertTrue(r["started1"] and r["started2"], r)
        self.assertEqual((r["recall1"]["outcome"], r["recall1"]["text"]), ("found", "Ответ b1"), r["recall1"])
        r2 = r["recall2_before_any_turn"]
        self.assertEqual((r2["outcome"], r2["text"]), ("found", "Ответ b1"), r2)
        self.assertEqual(r["recall3"]["text"], "Ответ b1b", r["recall3"])

    def test_B2_graceful_stop_mid_turn(self) -> None:
        r = self.scene("b2")
        print(f"B2 before={r['recall_before']} during={r['recall_during_turn']} right_after={r['recall_right_after_start']} "
              f"after_resume={r['recall_after_resume']} rows2={r['rows2']}")

    def test_B3_kill_inside_the_final_send(self) -> None:
        r = self.scene("b3")
        print(f"B3 held={r['held']} while_held={r['recall_while_final_is_held']} after_ledger={r['recall_after_ledger']} "
              f"2={r['recall_after_ledger_2']} 3={r['recall_after_ledger_3']} stats={r['stats_before_recall']} -> "
              f"{r['stats_after_recall']} rows2={r['rows2']}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        child_entry()
    else:
        unittest.main(verbosity=2)
