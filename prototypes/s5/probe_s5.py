"""Prototype S5: lifecycle of a loopback-listening platform adapter and recovery after a gateway restart.

What it answers (``docs/PLAN.md``, stage 1, S5): does the plugin (the future ``yandex_alice`` adapter),
through the public plugin API only, (L1) open no port on import, (L2) keep exactly one listener after
connect, (L3) keep it after ``reload-plugins``/enable, (L4) keep it across the gateway's own reconnect,
(L5) free the port on stop, (P1) leave the rest of the gateway working when the port is taken and
reconnect by itself once it is free, (P2) never log the secret path; and what reaches the adapter
after a gateway restart in the middle of a turn (R1-R5) - so the adapter can tell a recovered answer by
FORM and never by the English text of Hermes' marker.

How: the probe process (this module under ``unittest``) holds the scripted loopback LLM
(``tests/fakes/fake_llm_provider``) and the "foreign" listener, and spawns the GATEWAY as a child
process (``python probe_s5.py child``): a real ``GatewayRunner`` with ``multiplex_profiles`` (default +
``p2``, the route target of ``yandex_alice``). A "restart" is a new child process in the SAME throwaway
home: a graceful stop (``runner.stop()``) or ``SIGKILL``. Every restart scene has its own home (the
restart-loop guard counts boots per home). The child takes commands as JSON lines on stdin.

PROBE ONLY. ``gateway.run``, ``gateway.run_plugin_rewire``, ``tests.*`` and the runner's private
attributes are the harness, NOT the plugin API; the plugin itself (``plugin/``) imports only
``public-api.md`` modules and never reads ``os.environ``. Run through ``run.sh``. ``RecoveryTests``
need no Hermes: ``cd prototypes/s5 && python -m unittest probe_s5 -k RecoveryTests``. Python >= 3.11.

Scene assertions are strict only where the criterion is binary (L1-L5, P1, P2); the restart scenes
(R1-R5) record facts that the verdict in ``docs/RECON.md`` reads from the printed output.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
PLATFORM = "yandex_alice"
OTHER = "s5_other"
PLUGIN_NAME = "yandex-alice-s5"
P2 = "p2"
OWNER_ID = "u-probe-owner"  # synthetic
SECRET = "s5-synthetic-path-secret-0123456789"  # synthetic; the probe greps every log for it
BODY_MARK = "маркер-тела-S5"  # synthetic request text; the probe reports where it shows up
TOOLS_EAGER = "tools:\n  tool_search:\n    enabled: 'off'\n"
START_TIMEOUT = 150.0

# --- the pure layer, no Hermes ---------------------------------------------------------------------


def _load_recovery():
    spec = importlib.util.spec_from_file_location("s5_recovery", HERE / "plugin" / "recovery.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RECOVERED_MARKER = ("♻️ Recovered reply — the gateway restarted during delivery, "
                    "so this may be a duplicate:\n\n")
RECONNECTED_MARKER = ("♻️ Recovered reply — the messaging platform reconnected after the original "
                      "delivery failed, so this may be a duplicate:\n\n")
FLOOD_MARKER = ("♻️ Recovered reply — the messaging platform's rate limit refused the original, so part "
                "of it may already have arrived above:\n\n")


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.r = _load_recovery()

    def test_all_three_hermes_markers_are_cut_by_form(self) -> None:
        for marker in (RECOVERED_MARKER, RECONNECTED_MARKER, FLOOD_MARKER):
            self.assertEqual(self.r.strip_marker(marker + "Ответ готов."), "Ответ готов.")

    def test_marker_without_variation_selector(self) -> None:
        self.assertEqual(self.r.strip_marker("♻ Any words at all:\n\nОтвет"), "Ответ")

    def test_words_are_never_parsed(self) -> None:
        self.assertEqual(self.r.strip_marker("♻️ Совсем другие слова:\n\nОтвет"), "Ответ")
        self.assertEqual(self.r.strip_marker("Recovered reply — may be a duplicate:\n\nОтвет"),
                         "Recovered reply — may be a duplicate:\n\nОтвет")

    def test_not_marker_shaped_stays_whole(self) -> None:
        for text in ("Ответ", "", "♻️ без двоеточия\n\nОтвет", "♻️ Заголовок:",
                     "♻️ Заголовок:\n\n   ", "Ответ\n\n♻️ Заголовок:\n\nхвост"):
            self.assertEqual(self.r.strip_marker(text), text)

    def test_none_is_safe(self) -> None:
        self.assertEqual(self.r.strip_marker(None), "")

    def _kind(self, **kw) -> str:
        base = dict(open_turn=True, turn_mid="m1", reply_to="m1", notify=True, interim=False, text="Ответ")
        base.update(kw)
        return self.r.classify_send(**base)

    def test_final_is_the_s1_rule(self) -> None:
        self.assertEqual(self._kind(), self.r.FINAL)
        self.assertEqual(self._kind(text="⏳ Начало ответа"), self.r.FINAL)  # a prefix is not a veto
        self.assertEqual(self._kind(reply_to="m2"), self.r.OUTSIDE)
        self.assertEqual(self._kind(notify=None), self.r.OUTSIDE)
        self.assertEqual(self._kind(interim=True), self.r.INTERIM)

    def test_internal_turn_without_message_id(self) -> None:
        self.assertEqual(self._kind(turn_mid=None, reply_to=None), self.r.FINAL)
        self.assertEqual(self._kind(turn_mid=None, reply_to=None, notify=None), self.r.OUTSIDE)

    def test_recovered_only_outside_a_turn(self) -> None:
        marked = RECOVERED_MARKER + "Ответ"
        self.assertEqual(self._kind(open_turn=False, turn_mid=None, reply_to=None, notify=None, text=marked),
                         self.r.RECOVERED)
        # inside an open turn the agent's own text starting like a marker is a final, never cut
        self.assertEqual(self._kind(text="♻️ Итог:\n\nтекст"), self.r.FINAL)
        # a ledger redelivery of a never-started row has no marker: indistinguishable from other outside sends
        self.assertEqual(self._kind(open_turn=False, turn_mid=None, reply_to=None, notify=None), self.r.OUTSIDE)


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
    limit = int(os.environ.get("S5_RSS_LIMIT_MB") or 0)

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

    threading.Thread(target=loop, name="s5-rss-watchdog", daemon=True).start()


def _guard_temp_home() -> Path:
    import pwd  # POSIX only

    raw = os.environ.get("HERMES_HOME", "").strip()
    if not raw:
        raise SystemExit("refusing: HERMES_HOME is not set (run through run.sh)")
    home = Path(raw).resolve()
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if real_home == home or real_home in home.parents:
        raise SystemExit(f"refusing: HERMES_HOME {home} is inside the real home of this user")
    if not any(p.name.startswith("hya-s5-") for p in home.parents):
        raise SystemExit(f"refusing: HERMES_HOME {home} is not under a hya-s5-* temp dir")
    return home


def listeners(pid: Any, port: int) -> int:
    """LISTEN sockets of process ``pid`` on ``port`` (IPv4 and IPv6), from /proc."""
    inodes = set()
    try:
        for fd in Path(f"/proc/{pid}/fd").iterdir():
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
    except OSError:
        return -1
    count = 0
    for name in ("tcp", "tcp6"):
        try:
            lines = Path(f"/proc/net/{name}").read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            if len(f) > 9 and f[3] == "0A" and int(f[1].rsplit(":", 1)[1], 16) == port and f[9] in inodes:
                count += 1
    return count


def _port_is_free(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Foreign:
    """A foreign listener on a loopback port: accepts and closes at once, counts the connections."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self._closed = False
        threading.Thread(target=self._loop, name="s5-foreign", daemon=True).start()

    def _loop(self) -> None:
        while not self._closed:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            conn.close()

    def poke(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=3):
                return True
        except OSError:
            return False

    def close(self) -> None:
        self._closed = True
        try:
            # A thread blocked in accept() keeps the listening socket alive past close(): shut it down first.
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _text_of(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content or "")


def _last_user_text(body: dict) -> str:
    for msg in reversed(body.get("messages") or []):
        if msg.get("role") == "user":
            return _text_of(msg.get("content"))
    return ""


class Llm:
    """Scripted loopback LLM. Phase 1 = the first gateway run, phase 2 = after the restart."""

    def __init__(self) -> None:
        from tests.fakes import fake_llm_provider as f

        self.f = f
        self.phase = 1
        self.resume_mode = "text"
        self.slow2 = False
        self.victim = ""
        self.hold = threading.Event()
        self.hold2 = threading.Event()
        self.phase2_from = 0
        self.srv = f.FakeLLMServer(self)
        self.srv.start()
        self.base_url = self.srv.base_url

    def __call__(self, record: dict):
        f = self.f
        body = record.get("body") or {}
        msgs = body.get("messages") or []
        last = msgs[-1] if msgs else {}
        user = _last_user_text(body)
        if last.get("role") == "tool":
            return f.Text("Готово: команда выполнена.")
        # The auto-resume turn: Hermes' note, alone (stop) or after the question (kill). Other
        # "[System note" texts (first-turn tips) are not a resume.
        if "[System note: The previous turn was interrupted" in user:
            if self.resume_mode == "tool":
                return f.ToolCall("terminal", {"command": f"rm -r {self.victim}"})
            if self.slow2:
                self.hold2.wait(90)
            return f.Text("Ответ после перезапуска")
        if "[hold]" in user:
            self.hold.wait(300)
            return f.Text("Ответ первого запуска")
        if "[tool]" in user:
            return f.ToolCall("terminal", {"command": f"rm -r {self.victim}"})
        if "[quick]" in user:
            return f.Text("Быстрый ответ")
        return f.Text("unscripted")

    def mains(self) -> list:
        return [r for r in list(self.srv.requests) if r.get("kind") == "main"]

    def next_phase(self) -> None:
        self.hold.set()  # free the threads of the dead run; the next run gets a fresh event
        self.hold = threading.Event()
        self.phase = 2
        self.phase2_from = len(self.mains())

    def phase2_requests(self) -> list:
        out = []
        for r in self.mains()[self.phase2_from:]:
            body = r.get("body") or {}
            user = _last_user_text(body)
            out.append(dict(
                user_head=user[:160], messages=len(body.get("messages") or []),
                note_kind=("interactive" if "Report to the user" in user else
                           "continue" if "CONTINUE the interrupted task" in user else None),
                last_role=((body.get("messages") or [{}])[-1] or {}).get("role")))
        return out

    def stop(self) -> None:
        self.hold.set()
        self.hold2.set()
        self.srv.stop()


class Stand:
    """One throwaway gateway home: default profile + p2, the plugin, the journal."""

    def __init__(self, root: Path, name: str, llm: Llm, *, interactive: bool, notify: bool,
                 port: Optional[int] = None, log_level: str = "INFO") -> None:
        self.name = name
        self.llm = llm
        self.interactive = interactive
        self.notify = notify
        self.log_level = log_level
        self.port = port or _free_port()
        self.base = root / name
        self.home = self.base / "home" / ".hermes"
        self.victim = self.base / "victim"
        self.journal = self.home / "s5-journal.jsonl"

    def write(self) -> None:
        from tests.fakes import fake_llm_provider as f

        for sub in ("locks", "tmp", "victim"):
            (self.base / sub).mkdir(parents=True, exist_ok=True)
        (self.victim / "keep.txt").write_text("probe", encoding="utf-8")
        p2 = self.home / "profiles" / P2
        yes = lambda v: "true" if v else "false"  # noqa: E731
        f.write_hermes_home(self.home, self.llm.base_url, extra_config=(
            "gateway:\n  multiplex_profiles: true\n  loop_watchdog: false\n"
            f"  profile_routes:\n    - name: alice-probe\n      platform: {PLATFORM}\n      profile: {P2}\n"
            f"plugins:\n  enabled:\n    - {PLUGIN_NAME}\n"
            f"logging:\n  level: {self.log_level}\n"
            f"platforms:\n  {PLATFORM}:\n    enabled: true\n    gateway_restart_notification: {yes(self.notify)}\n"
            f"    extra:\n      port: {self.port}\n      interactive_resume: {yes(self.interactive)}\n"
            f"  {OTHER}:\n    enabled: true\n"
            "approvals:\n  mode: manual\n  timeout: 300\n" + TOOLS_EAGER))
        f.write_hermes_home(p2, self.llm.base_url, extra_config=(
            f"platform_toolsets:\n  {PLATFORM}: [terminal]\napprovals:\n  mode: manual\n  timeout: 20\n"
            "display:\n  busy_input_mode: interrupt\n" + TOOLS_EAGER))
        for h in (self.home, p2):
            cfg = h / "config.yaml"
            text = cfg.read_text(encoding="utf-8")
            if text.count("\nagent:\n") != 1:
                raise AssertionError(f"expected exactly one agent: section in {cfg}")
            cfg.write_text(text.replace("\nagent:\n", "\nagent:\n  auto_recovery_cycles: 0\n", 1), encoding="utf-8")
        with (self.home / ".env").open("a", encoding="utf-8") as fh:
            fh.write(f"ALICE_PATH_SECRET={SECRET}\nALICE_OWNER_USER_ID={OWNER_ID}\n")
        shutil.copytree(HERE / "plugin", self.home / "plugins" / PLUGIN_NAME,
                        ignore=shutil.ignore_patterns("__pycache__"))
        (self.home / "s5-control").mkdir(exist_ok=True)

    def env(self) -> dict:
        env = dict(os.environ)
        env.update(HOME=str(self.base / "home"), HERMES_HOME=str(self.home),
                   HERMES_GATEWAY_LOCK_DIR=str(self.base / "locks"), TMPDIR=str(self.base / "tmp"),
                   S5_PORT=str(self.port))
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

    def post(self, payload: dict, path: Optional[str] = None, timeout: float = 40.0) -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        t0 = time.monotonic()
        try:
            conn.request("POST", "/" + (SECRET if path is None else path), body=json.dumps(payload),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            resp.read()
            return dict(status=resp.status, took=round(time.monotonic() - t0, 3))
        except OSError as exc:
            return dict(status=None, err=type(exc).__name__, took=round(time.monotonic() - t0, 3))
        finally:
            conn.close()

    def turn(self, mid: str, text: str = "[quick] привет", chat: str = "chat-1", timeout: float = 40.0) -> dict:
        start = len(self.rows())
        http_res = self.post({"text": text, "mid": mid, "chat": chat})
        done = self.wait_row(lambda r: r["op"] == "complete" and r.get("mid") == mid, timeout, start)
        sends = [r for r in self.rows()[start:] if r["op"] == "send" and r["chat"] == chat]
        finals = [r for r in sends if r["kind"] == "final" and r["reply_to"] == mid and r["notify"] is True]
        return dict(http=http_res, complete=done["outcome"] if done else None,
                    kinds=[r["kind"] for r in sends], finals=[r["head"] for r in finals])

    def log_text(self, extra_lines: list) -> dict:
        """Every log the run left: files under logs/, the child's stdout/stderr, the journal."""
        parts = {}
        log_dir = self.home / "logs"
        for path in sorted(log_dir.rglob("*")) if log_dir.is_dir() else []:
            if path.is_file():
                parts[str(path.relative_to(self.home))] = path.read_text(encoding="utf-8", errors="replace")
        parts["child-output"] = "\n".join(extra_lines)
        parts["journal"] = self.journal.read_text(encoding="utf-8", errors="replace") if self.journal.exists() else ""
        return parts


class Child:
    """The gateway as a child process; JSON-line commands on stdin, ``S5 {...}`` events on stdout."""

    def __init__(self, stand: Stand, mode: str = "gateway") -> None:
        self.stand = stand
        self.events: list = []
        self.lines: list = []
        self._cv = threading.Condition()
        self.eof = False
        self._next = 0
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-u", str(HERE / "probe_s5.py"), "child", mode], env=stand.env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            cwd=str(stand.base))
        self.pid = self.proc.pid
        self.t_start = time.monotonic()
        LIVE.add(self)
        threading.Thread(target=self._read, name="s5-child-reader", daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("S5 "):
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
                if left <= 0 or self.eof:  # eof: the reader drained the pipe, nothing more will come
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

    def interesting_lines(self, pattern: str, limit: int = 30) -> list:
        rx = re.compile(pattern, re.I)
        return [ln[:220] for ln in self.lines if rx.search(ln)][:limit]


def start_gateway(stand: Stand) -> tuple:
    child = Child(stand)
    started = child.wait_event("started", START_TIMEOUT)
    return child, started


def stop_gracefully(child: Child, timeout: float = 90.0) -> dict:
    t0 = time.monotonic()
    resp = child.cmd("stop", timeout)
    exited = child.wait_exit(20) if resp else False
    forced = False
    if not exited:
        child.kill()
        forced = True
    return dict(answered=bool(resp), took=round(time.monotonic() - t0, 2), forced=forced)


def _compact(rows: list, t0: float) -> list:
    out = []
    for r in rows:
        r = dict(r)
        r["t"] = round(r["t"] - t0, 2)
        r.pop("pid", None)
        out.append(r)
    return out


# --- group A: lifecycle -----------------------------------------------------------------------------


def run_lifecycle(root: Path, llm: Llm) -> dict:
    stand = Stand(root, "life", llm, interactive=False, notify=False)
    stand.write()
    res: dict = {"port": "ephemeral"}
    # L1: import and construct without connect: no port.
    c = Child(stand, "import")
    res["L1"] = c.wait_event("import", START_TIMEOUT)
    c.wait_exit(30)
    c.close()
    # L2: start -> one listener, a turn works.
    c, started = start_gateway(stand)
    res["started"] = started
    res["L2"] = dict(listeners=listeners(c.pid, stand.port), turn=stand.turn("m-l2"))
    info0 = c.cmd("info")
    # L3: reload twice (the control-socket verb; enabling a plugin fires the same event).
    rows0 = len(stand.rows())
    reloads = [c.cmd("reload", 60), c.cmd("reload", 60)]
    info1 = c.cmd("info")
    pid_rows = [r for r in stand.rows()[rows0:] if r["pid"] == c.pid]
    res["L3"] = dict(
        reloads=reloads, adapter_before=info0 and info0.get("alice_id"), adapter_after=info1 and info1.get("alice_id"),
        listeners=listeners(c.pid, stand.port), turn=stand.turn("m-l3"),
        ops_during_reload={op: sum(1 for r in pid_rows if r["op"] == op)
                           for op in sorted({r["op"] for r in pid_rows})})
    # R6 (negative): an exception in the handler -> the error text goes out OUTSIDE the closed turn.
    c.cmd("arm_boom")
    start = len(stand.rows())
    http_res = stand.post({"text": "[boom] сбой", "mid": "m-boom", "chat": "chat-boom"})
    done = stand.wait_row(lambda r: r["op"] == "complete" and r.get("mid") == "m-boom", 30, start)
    time.sleep(3)
    res["R6"] = dict(http=http_res, complete=done and done["outcome"],
                     sends=[{k: r[k] for k in ("kind", "open_turn", "reply_to", "notify", "interim", "meta_keys",
                                               "head", "marker")} for r in stand.rows()[start:]
                            if r["op"] == "send" and r["chat"] == "chat-boom"])
    # L4: the gateway's own reconnect after a retryable fatal error.
    rows0 = len(stand.rows())
    t0 = time.monotonic()
    res["L4_fatal"] = c.cmd("fatal", 30)
    row = stand.wait_row(lambda r: r["op"] == "connect" and r["ok"] and r.get("is_reconnect") and r["pid"] == c.pid,
                         200, rows0)
    pid_rows = [r for r in stand.rows()[rows0:] if r["pid"] == c.pid]
    res["L4"] = dict(
        reconnected=row is not None, after_s=round(time.monotonic() - t0, 1), listeners=listeners(c.pid, stand.port),
        creates=sum(1 for r in pid_rows if r["op"] == "create"), disconnects=sum(1 for r in pid_rows if r["op"] == "disconnect"),
        new_adapter=row and row["adapter_id"] != (info1 or {}).get("alice_id"), turn=stand.turn("m-l4"),
        info=c.cmd("info"))
    # L5: graceful stop frees the port.
    res["L5"] = dict(stop=stop_gracefully(c), port_free=_port_is_free(stand.port))
    # L6: SIGKILL, then an immediate new gateway on the same port.
    c2, st2 = start_gateway(stand)
    pid2_listen = listeners(c2.pid, stand.port)
    c2.kill()
    c3, st3 = start_gateway(stand)
    res["L6"] = dict(first_started=bool(st2 and st2.get("ok")), first_listeners=pid2_listen,
                     second_started=bool(st3 and st3.get("ok")), second_listeners=listeners(c3.pid, stand.port),
                     turn=stand.turn("m-l6"))
    stop_gracefully(c3)
    res["rss"] = dict(RSS_PEAK)
    res["logs"] = {k: (SECRET in v) for k, v in stand.log_text(c.lines + c3.lines).items()}
    return res


# --- group B: busy port -----------------------------------------------------------------------------


def run_busy_port(root: Path, llm: Llm) -> dict:
    foreign = Foreign()
    stand = Stand(root, "busy", llm, interactive=False, notify=False, port=foreign.port, log_level="DEBUG")
    stand.write()
    res: dict = {}
    c, started = start_gateway(stand)
    res["started"] = started
    res["alice_listeners"] = listeners(c.pid, stand.port)
    res["gateway_alive"] = c.alive()
    # the rest of the gateway works
    start = len(stand.rows())
    res["other_cmd"] = c.cmd("turn_other", 40, text="[quick] привет", mid="m-other")
    done = stand.wait_row(lambda r: r["op"] == "other_complete" and r.get("mid") == "m-other", 40, start)
    res["other_turn"] = dict(complete=done and done["outcome"],
                             sends=[r["head"] for r in stand.rows()[start:] if r["op"] == "other_send"])
    # the foreign socket is untouched
    before = foreign.accepted
    res["foreign_poke"] = foreign.poke()
    time.sleep(0.5)
    res["foreign_accepts_after_poke"] = foreign.accepted - before
    # the gateway's retry
    t_start = time.monotonic()
    second = stand.wait_row(lambda r: r["op"] == "connect" and not r["ok"] and r.get("is_reconnect"), 200)
    res["retry_after_s"] = round(time.monotonic() - t_start, 1) if second else None
    fails = [r for r in stand.rows() if r["op"] == "connect" and not r["ok"]]
    res["bind_failures"] = [dict(errno=r.get("errno"), is_reconnect=r.get("is_reconnect")) for r in fails]
    res["info_during"] = c.cmd("info")
    # wrong path / valid path while failing is impossible (no listener of ours): the foreign one answers nothing.
    # free the port: the gateway reconnects by itself
    foreign.close()
    t_free = time.monotonic()
    ok = stand.wait_row(lambda r: r["op"] == "connect" and r["ok"] and r["pid"] == c.pid, 300)
    res["recovered_after_s"] = round(time.monotonic() - t_free, 1) if ok else None
    res["alice_listeners_after"] = listeners(c.pid, stand.port)
    res["info_after"] = c.cmd("info")
    # P2: valid and wrong paths, then grep every log
    res["turn_after"] = stand.turn("m-after", text=f"[quick] {BODY_MARK}")
    res["wrong_path"] = stand.post({"text": "x", "mid": "m-bad"}, path="wrong-" + SECRET[::-1])
    res["bad_body"] = stand.post({"nope": 1})
    time.sleep(1.5)
    logs = stand.log_text(c.lines)
    res["P2"] = dict(secret_in={k: (SECRET in v) for k, v in logs.items()},
                     body_mark_in=sorted(k for k, v in logs.items() if BODY_MARK in v and k != "journal"),
                     sizes={k: len(v) for k, v in logs.items()})
    res["lines_bind"] = c.interesting_lines(r"bind_failed|yandex_alice|reconnect")
    res["stop"] = stop_gracefully(c)
    res["rss"] = dict(RSS_PEAK)
    return res


# --- groups C/D: restart scenes ---------------------------------------------------------------------

GUARD_RX = r"loop guard|breaker|Skipping auto-resume|stuck"
RESUME_RX = r"resum|recover|obligation|redeliver|interrupt|restart"


def _observe(stand: Stand, child: Child, mark: int, timeout: float = 60.0) -> list:
    """Rows the new run writes after boot, until the first visible outcome (+ a settle window)."""
    def mine(rows):
        return [r for r in rows[mark:] if r["pid"] == child.pid]

    deadline = time.monotonic() + timeout
    first = None
    while time.monotonic() < deadline and first is None:
        first = next((r for r in mine(stand.rows()) if r["op"] in ("complete", "approval_prompt", "send")), None)
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


def restart_scene(root: Path, llm: Llm, name: str, *, interactive: bool, notify: bool, how: str, phase1: str,
                  resume_mode: str = "text", slow2: bool = False) -> dict:
    stand = Stand(root, name, llm, interactive=interactive, notify=notify)
    stand.write()
    llm.phase, llm.resume_mode, llm.slow2, llm.victim = 1, resume_mode, slow2, str(stand.victim)
    llm.hold, llm.hold2 = threading.Event(), threading.Event()
    res: dict = dict(name=name, interactive=interactive, notify=notify, how=how, phase1=phase1)
    mid, chat = "m-" + name, "chat-" + name
    control = stand.home / "s5-control"
    c1, started1 = start_gateway(stand)
    res["started1"] = bool(started1 and started1.get("ok"))
    text = {"hold": "[hold] вопрос", "tool": "[tool] удали", "final_hold": "[quick] вопрос"}[phase1]
    if phase1 == "final_hold":
        (control / "hold_final").write_text("1", encoding="utf-8")
    n_before = len(llm.mains())
    res["post1"] = stand.post({"text": text, "mid": mid, "chat": chat})
    marker = {"hold": lambda r: r["op"] == "start" and r.get("mid") == mid,
              "tool": lambda r: r["op"] == "approval_prompt",
              "final_hold": lambda r: r["op"] == "held"}[phase1]
    res["phase1_reached"] = stand.wait_row(marker, 60) is not None
    if phase1 == "hold":
        deadline = time.monotonic() + 30
        while len(llm.mains()) <= n_before and time.monotonic() < deadline:
            time.sleep(0.1)
    res["phase1_rows"] = _compact([r for r in stand.rows() if r["pid"] == c1.pid], c1.t_start)
    t_kill = time.monotonic()
    if how == "stop":
        res["stop"] = stop_gracefully(c1)
    else:
        c1.kill()
    res["down_after_s"] = round(time.monotonic() - t_kill, 2)
    (control / "hold_final").unlink(missing_ok=True)
    llm.next_phase()
    mark = len(stand.rows())
    c2, started2 = start_gateway(stand)
    res["started2"] = bool(started2 and started2.get("ok"))
    t2 = c2.t_start
    if slow2:  # R5: a new phrase arrives while the resumed turn is still running
        stand.wait_row(lambda r: r["op"] == "start" and r["pid"] == c2.pid, 60, mark)
        res["intruder"] = stand.post({"text": "[quick] второй вопрос", "mid": "m-intruder", "chat": chat})
        time.sleep(5)
        res["rows_before_release"] = _compact([r for r in stand.rows()[mark:] if r["pid"] == c2.pid], t2)
        llm.hold2.set()
    rows2 = _observe(stand, c2, mark)
    res["phase2_rows"] = _compact(rows2, t2)
    res["phase2_llm"] = llm.phase2_requests()
    res["victim_exists"] = stand.victim.exists()
    res["guard_lines"] = c2.interesting_lines(GUARD_RX)
    res["resume_lines"] = c2.interesting_lines(RESUME_RX)
    res["tail_lines"] = c2.lines[-8:]
    res["info"] = c2.cmd("info", 20)
    res["stop2"] = stop_gracefully(c2)
    res["rss"] = dict(RSS_PEAK)
    c1.close()
    c2.close()
    return res


def run_restart_group(root: Path, llm: Llm, scenes: list, *, interactive: bool, notify: bool) -> dict:
    out = {}
    for name, how, phase1, kw in scenes:
        try:
            out[name] = restart_scene(root, llm, name, interactive=interactive, notify=notify, how=how,
                                      phase1=phase1, **kw)
        except Exception as exc:  # noqa: BLE001 - recorded, asserted by the test of that scene
            out[name] = dict(name=name, error=f"{type(exc).__name__}: {exc}")
    return out


SCENES_C = [
    ("r1", "stop", "hold", {}),
    ("r2", "kill", "hold", {}),
    ("r3", "kill", "final_hold", {}),
    ("r4", "kill", "tool", dict(resume_mode="tool")),
    ("r5", "stop", "hold", dict(slow2=True)),
]
SCENES_D = [
    ("d1", "stop", "hold", {}),
    ("d2", "kill", "hold", {}),
]


# --- the child: the gateway ------------------------------------------------------------------------


def emit(**kw: Any) -> None:
    print("S5 " + json.dumps(kw, ensure_ascii=False, default=str), flush=True)


def _import_check() -> None:
    from gateway.config import PlatformConfig
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import discover_plugins

    port = int(os.environ["S5_PORT"])
    discover_plugins(force=True)
    entry = platform_registry.get(PLATFORM)
    before = listeners(os.getpid(), port)
    adapter = entry.adapter_factory(PlatformConfig(enabled=True, extra={"port": port, "interactive_resume": False}))
    after = listeners(os.getpid(), port)
    s = socket.socket()
    s.settimeout(2)
    refused = s.connect_ex(("127.0.0.1", port)) != 0
    s.close()
    emit(ev="import", found=entry is not None, listeners_after_discover=before, listeners_after_factory=after,
         connect_refused=refused, constructed=type(adapter).__name__)


async def _child_main() -> None:
    import asyncio

    from gateway.config import Platform, load_gateway_config
    from gateway.run import GatewayRunner
    from gateway.run_plugin_rewire import reload_plugins_verb

    # As `hermes gateway run` does: file handlers (agent.log, gateway.log, errors.log) at logging.level.
    # Without this the probe's logs would be empty and "no secret in the logs" would prove nothing.
    from hermes_logging import setup_logging

    setup_logging(mode="gateway")
    runner = GatewayRunner(load_gateway_config())
    ok = await runner.start()
    loop = asyncio.get_running_loop()

    def alice():
        return runner.adapters.get(Platform(PLATFORM))

    def name_of(key) -> str:
        return str(getattr(key, "value", key))

    def failed() -> dict:
        return {name_of(k): dict(attempts=v.get("attempts"), paused=bool(v.get("paused")))
                for k, v in (getattr(runner, "_failed_platforms", None) or {}).items()}

    emit(ev="started", ok=bool(ok), alice=alice() is not None, adapters=sorted(name_of(k) for k in runner.adapters),
         failed=failed())

    async def handle(cmd: dict) -> dict:
        op = cmd["op"]
        if op == "info":
            a = alice()
            return dict(alice_id=id(a) if a else None, failed=failed(), adapters=sorted(name_of(k) for k in runner.adapters))
        if op == "reload":
            result = await loop.run_in_executor(None, reload_plugins_verb(runner, loop), {})
            return dict(result={k: v for k, v in result.items() if k in ("reloaded", "adapters_rewired", "error")})
        if op == "fatal":
            a = alice()
            a._set_fatal_error("s5_forced", "forced by the probe", retryable=True)
            await a._notify_fatal_error()
            return {}
        if op == "arm_boom":
            a = alice()
            inner = a._message_handler

            async def boom(event):
                if "[boom]" in (event.text or ""):
                    raise RuntimeError("probe boom")
                return await inner(event)

            a.set_message_handler(boom)
            return {}
        if op == "turn_other":
            await runner.adapters[Platform(OTHER)].probe_inject(cmd["text"], cmd["mid"], "chat-other")
            return {}
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
            emit(ev="resp", id=cmd["id"], ok=True, **out)
        except Exception as exc:  # noqa: BLE001
            emit(ev="resp", id=cmd["id"], ok=False, err=f"{type(exc).__name__}: {exc}")
        if cmd["op"] == "stop":
            os._exit(0)


def child_entry(mode: str) -> None:
    import asyncio

    sys.path.insert(0, os.environ["S5_CHECKOUT"])
    sys._hermes_pin_default_home = True
    import hermes_bootstrap  # noqa: F401  (sets up the Hermes runtime site-packages)

    _guard_temp_home()
    if mode == "import":
        _import_check()
        return
    asyncio.run(_child_main())


# --- groups under unittest -----------------------------------------------------------------------------


def _dump(title: str, res: dict) -> None:
    print(f"\n=== {title}")
    print(json.dumps(res, ensure_ascii=False, default=str, indent=1)[:24000])


class _Base(unittest.TestCase):
    res: dict = {}
    llm: Llm

    @classmethod
    def _root(cls) -> Path:
        home = _guard_temp_home()
        return home.parents[1]  # <hya-s5-*>/root-<G>; the run.sh home is <root>/home/.hermes

    def test_00_rss(self) -> None:
        print(f"rss_peak_total_mb={RSS_PEAK['mb']} probe_only_mb={RSS_PEAK['self']}")
        self.assertGreater(RSS_PEAK["mb"], 0)


class LifecycleGroup(_Base):
    @classmethod
    def setUpClass(cls) -> None:
        _start_watchdog()
        cls.llm = Llm()
        try:
            cls.res = run_lifecycle(cls._root() / "scenes", cls.llm)
        finally:
            cls.llm.stop()
        _dump("group A: lifecycle", cls.res)

    def test_L1_import_opens_no_port(self) -> None:
        r = self.res["L1"]
        self.assertIsNotNone(r)
        self.assertTrue(r["found"])
        self.assertEqual((r["listeners_after_discover"], r["listeners_after_factory"]), (0, 0))
        self.assertTrue(r["connect_refused"])

    def test_L2_start_one_listener_and_a_turn(self) -> None:
        self.assertTrue(self.res["started"]["ok"], self.res["started"])
        self.assertEqual(self.res["L2"]["listeners"], 1)
        turn = self.res["L2"]["turn"]
        self.assertEqual(turn["http"]["status"], 200)
        self.assertEqual(turn["complete"], "success")
        self.assertEqual(turn["finals"], ["Быстрый ответ"])

    def test_L3_reload_keeps_the_one_listener(self) -> None:
        r = self.res["L3"]
        self.assertTrue(all(x and x["ok"] for x in r["reloads"]), r["reloads"])
        self.assertEqual(r["adapter_before"], r["adapter_after"])
        self.assertEqual(r["listeners"], 1)
        self.assertEqual(r["ops_during_reload"].get("create", 0), 0, r)
        self.assertEqual(r["ops_during_reload"].get("disconnect", 0), 0, r)
        self.assertEqual(r["turn"]["finals"], ["Быстрый ответ"])

    def test_L4_reconnect_keeps_the_one_listener(self) -> None:
        r = self.res["L4"]
        self.assertTrue(r["reconnected"], r)
        self.assertEqual(r["listeners"], 1)
        self.assertEqual((r["creates"], r["disconnects"]), (1, 1))
        self.assertTrue(r["new_adapter"])
        self.assertEqual(r["turn"]["finals"], ["Быстрый ответ"])

    def test_L5_stop_frees_the_port(self) -> None:
        self.assertFalse(self.res["L5"]["stop"]["forced"], self.res["L5"])
        self.assertTrue(self.res["L5"]["port_free"])

    def test_L6_kill_then_restart_on_the_same_port(self) -> None:
        r = self.res["L6"]
        self.assertTrue(r["first_started"] and r["second_started"], r)
        self.assertEqual((r["first_listeners"], r["second_listeners"]), (1, 1))
        self.assertEqual(r["turn"]["finals"], ["Быстрый ответ"])

    def test_R6_error_after_the_closed_turn_is_reported(self) -> None:
        r = self.res["R6"]  # facts only: the metadata decide whether such a send can be told from a recovered reply
        self.assertEqual(r["http"]["status"], 200, r)
        print(f"R6: complete={r['complete']} sends={r['sends']}")

    def test_P2_no_secret_in_any_log(self) -> None:
        self.assertIn("logs/agent.log", self.res["logs"], "no agent.log: the check would prove nothing")
        self.assertFalse([k for k, v in self.res["logs"].items() if v], self.res["logs"])

    def test_validate(self) -> None:
        _validate_plugin(self)


def _validate_plugin(case: unittest.TestCase) -> None:
    """``hermes plugins validate`` on the probe plugin: imports outside public-api.md."""
    try:
        from hermes_cli.plugin_validate import validate_plugin_dir

        report = validate_plugin_dir(HERE / "plugin", None)
        v = dict(ok=bool(report.ok), warnings=[str(w) for w in report.warnings])
    except Exception as exc:  # noqa: BLE001
        v = dict(error=f"{type(exc).__name__}: {exc}")
    print(f"validate={json.dumps(v, ensure_ascii=False)}")
    case.assertNotIn("error", v, v)
    case.assertFalse([w for w in v["warnings"] if "import" in w.lower()], v["warnings"])


class PortGroup(_Base):
    @classmethod
    def setUpClass(cls) -> None:
        _start_watchdog()
        cls.llm = Llm()
        try:
            cls.res = run_busy_port(cls._root() / "scenes", cls.llm)
        finally:
            cls.llm.stop()
        _dump("group B: busy port", cls.res)

    def test_P1_gateway_survives_and_the_rest_works(self) -> None:
        r = self.res
        self.assertIsNotNone(r["started"], "the child never reported the start")
        self.assertTrue(r["gateway_alive"])
        self.assertEqual(r["alice_listeners"], 0)
        self.assertEqual(r["other_turn"]["complete"], "success", r["other_turn"])

    def test_P1_foreign_socket_untouched(self) -> None:
        self.assertTrue(self.res["foreign_poke"])
        self.assertEqual(self.res["foreign_accepts_after_poke"], 1)

    def test_P1_failure_is_retryable_and_never_paused(self) -> None:
        r = self.res
        self.assertGreaterEqual(len(r["bind_failures"]), 2, r["bind_failures"])
        self.assertTrue(all(f["errno"] == 98 for f in r["bind_failures"]), r["bind_failures"])
        self.assertTrue(r["bind_failures"][-1]["is_reconnect"])
        info = r["info_during"] or {}
        queued = (info.get("failed") or {}).get(PLATFORM)
        self.assertIsNotNone(queued, info)
        self.assertFalse(queued["paused"])

    def test_P1_reconnects_by_itself_once_free(self) -> None:
        r = self.res
        self.assertIsNotNone(r["recovered_after_s"], "no reconnect within 300 s")
        self.assertEqual(r["alice_listeners_after"], 1)
        self.assertEqual(r["turn_after"]["finals"], ["Быстрый ответ"])
        self.assertEqual(r["wrong_path"]["status"], 404)
        self.assertEqual(r["bad_body"]["status"], 400)  # right path, malformed body
        self.assertFalse((r["info_after"] or {}).get("failed"), r["info_after"])

    def test_P2_no_secret_in_any_log_at_debug(self) -> None:
        r = self.res["P2"]
        self.assertGreater(r["sizes"].get("logs/agent.log", 0), 0, "no agent.log: the check would prove nothing")
        self.assertFalse([k for k, v in r["secret_in"].items() if v], r)
        print(f"body mark found in: {r['body_mark_in']} (Hermes core, not the plugin, if any)")


class _RestartBase(_Base):
    SCENES: list = []
    INTERACTIVE = False
    NOTIFY = False

    @classmethod
    def setUpClass(cls) -> None:
        _start_watchdog()
        cls.llm = Llm()
        try:
            cls.res = run_restart_group(cls._root() / "scenes", cls.llm, cls.SCENES,
                                        interactive=cls.INTERACTIVE, notify=cls.NOTIFY)
        finally:
            cls.llm.stop()
        _dump(f"restart scenes (interactive_resume={cls.INTERACTIVE}, restart notification={cls.NOTIFY})", cls.res)

    def scene(self, name: str) -> dict:
        r = self.res[name]
        self.assertNotIn("error", r, r.get("error"))
        self.assertTrue(r["started1"] and r["started2"], r)
        self.assertTrue(r["phase1_reached"], f"the first run never reached its marker: {r['phase1_rows']}")
        self.assertEqual(r["guard_lines"], [], "a loop guard fired: the scene proves nothing")
        return r


class RestartGroup(_RestartBase):
    SCENES = SCENES_C

    def test_R1_graceful_stop_mid_turn(self) -> None:
        r = self.scene("r1")
        print(f"R1 stop={r['stop']} rows2={r['phase2_rows']} llm2={r['phase2_llm']}")

    def test_R2_sigkill_mid_turn(self) -> None:
        r = self.scene("r2")
        print(f"R2 rows2={r['phase2_rows']} llm2={r['phase2_llm']}")

    def test_R3_sigkill_inside_the_final_send(self) -> None:
        r = self.scene("r3")
        print(f"R3 rows2={r['phase2_rows']} llm2={r['phase2_llm']}")

    def test_R4_sigkill_while_waiting_for_approval(self) -> None:
        r = self.scene("r4")
        prompts1 = [x for x in r["phase1_rows"] if x["op"] == "approval_prompt"]
        prompts2 = [x for x in r["phase2_rows"] if x["op"] == "approval_prompt"]
        print(f"R4 first={prompts1} second={prompts2} victim_exists={r['victim_exists']}")
        self.assertTrue(r["victim_exists"], "the dangerous command ran without anybody approving it")

    def test_R5_new_phrase_during_the_resumed_turn(self) -> None:
        r = self.scene("r5")
        print(f"R5 intruder={r['intruder']} before={r['rows_before_release']} rows2={r['phase2_rows']}")

    def test_validate(self) -> None:
        _validate_plugin(self)


class DefaultResumeGroup(_RestartBase):
    SCENES = SCENES_D
    INTERACTIVE = True
    NOTIFY = True

    def test_D1_graceful_stop_default_settings(self) -> None:
        r = self.scene("d1")
        print(f"D1 stop={r['stop']} rows2={r['phase2_rows']} llm2={r['phase2_llm']}")

    def test_D2_sigkill_default_settings(self) -> None:
        r = self.scene("d2")
        print(f"D2 rows2={r['phase2_rows']} llm2={r['phase2_llm']}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        child_entry(sys.argv[2] if len(sys.argv) > 2 else "gateway")
    else:
        unittest.main(verbosity=2)
