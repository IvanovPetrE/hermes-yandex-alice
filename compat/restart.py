"""Рестарты шлюза для тестов совместимости (этап 6, срез «в»; S5): дочерний процесс шлюза в доме сцены.

`RestartWorld` — тот же `World` (скриптованный агент в процессе теста, реплики Алисы по HTTP), но шлюз — дочерний процесс
`gateway_child.py`: его можно остановить штатно или убить (`SIGKILL`) и запустить заново в том же доме. Каждая сцена — свой дом
(`<корень прогона>/scenes/<имя>`): счётчик перезапусков Hermes считается по дому. Только для `compat/`.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import harness

START_TIMEOUT = 150.0
HERE = Path(__file__).resolve().parent


def checkout_dir() -> str:
    import tests.fakes

    return str(Path(tests.fakes.__file__).resolve().parents[2])


def scene_home(name: str) -> Path:
    """`<hya-compat-*>/scenes/<имя>/home/.hermes`; рядом — locks и tmp (то, что `run.sh` задаёт для прогона)."""
    root = harness.temp_home().parents[1] / "scenes" / name
    for sub in ("home/.hermes", "locks", "tmp"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root / "home" / ".hermes"


def listeners(pid: Any, port: int) -> int:
    """Сокеты LISTEN процесса `pid` на `port` (IPv4 и IPv6), по /proc."""
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


def port_is_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


class Foreign:
    """Чужой listener на loopback-порту: принимает и сразу закрывает, считает соединения."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        threading.Thread(target=self._loop, name="compat-foreign", daemon=True).start()

    def _loop(self) -> None:
        while True:
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
        try:
            self.sock.shutdown(socket.SHUT_RDWR)   # поток в accept() держит сокет живым даже после close()
        except OSError:
            pass
        self.sock.close()


class Child:
    """Шлюз как процесс: события `HYA {...}` из stdout, команды JSON-строками в stdin."""

    def __init__(self, world: "RestartWorld") -> None:
        env = dict(os.environ)
        base = world.home.parents[1]   # <корень сцены>: рядом с home/ лежат locks/ и tmp/
        env.update(HOME=str(world.home.parent), HERMES_HOME=str(world.home),
                   HERMES_GATEWAY_LOCK_DIR=str(base / "locks"), TMPDIR=str(base / "tmp"))
        self.events: list = []
        self.lines: list = []
        self._cv = threading.Condition()
        self.eof = False
        self._next = 0
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-u", str(HERE / "gateway_child.py"), checkout_dir()], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            cwd=str(base))
        self.pid = self.proc.pid
        threading.Thread(target=self._read, name="compat-child-reader", daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("HYA "):
                try:
                    event = json.loads(line[4:])
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
        """`SIGKILL` — только этому дочернему шлюзу."""
        try:
            self.proc.kill()
        except OSError:
            pass
        self.wait_exit(15)


class RestartWorld(harness.World):
    """`World` с шлюзом-дочерним процессом; `self.journal` — строки JSONL всех запусков шлюза в доме."""

    def __init__(self, name: str, **kw: Any) -> None:
        super().__init__(home=scene_home(name), **kw)
        self.children: list = []

    @property
    def journal(self) -> list:   # type: ignore[override]
        rows = []
        try:
            for line in (self.home / "compat-journal.jsonl").read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(harness.Op(**json.loads(line)))
                except (ValueError, TypeError):
                    pass
        except OSError:
            pass
        return rows

    @journal.setter
    def journal(self, _value: list) -> None:   # World.__init__ присваивает список; здесь журнал — файл
        pass

    async def _gateway(self, scenarios) -> None:
        try:
            for name, fn in scenarios:
                try:
                    await asyncio.wait_for(fn(self), timeout=harness.TURN_TIMEOUT * 6)
                except Exception as exc:   # записывается, проверяется тестом сцены
                    self.errors[name] = f"{type(exc).__name__}: {exc}"
        finally:
            for child in self.children:
                if child.alive():
                    child.kill()

    # --- шлюз -----------------------------------------------------------------------------------------------------

    async def start(self) -> Child:
        child = Child(self)
        self.children.append(child)
        started = await asyncio.to_thread(child.wait_event, "started", START_TIMEOUT)
        if not started or not started.get("ok"):
            raise AssertionError(f"шлюз не стартовал: {started} {child.lines[-5:]}")
        return child

    async def stop(self, child: Child) -> bool:
        """Штатная остановка; True — процесс вышел сам, без `kill`."""
        resp = await asyncio.to_thread(child.cmd, "stop", 90.0)
        if resp and await asyncio.to_thread(child.wait_exit, 20):
            return True
        child.kill()
        return False

    async def kill(self, child: Child) -> None:
        await asyncio.to_thread(child.kill)

    # --- журнал и реплики -----------------------------------------------------------------------------------------

    async def wait_op(self, pred, timeout: float = 60.0, start: int = 0) -> Optional[harness.Op]:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for op in self.journal[start:]:
                if pred(op):
                    return op
            await asyncio.sleep(0.1)
        return None

    async def confirm_until(self, name: str, done, rounds: int = 20) -> str:
        """«Да» на каждое «ещё думаю», пока ответ не станет таким, что `done(reply)` истинно; все реплики — в `voice[name]`."""
        said = self.voice.setdefault(name, [])
        for _ in range(rounds):
            reply = await self.say("да", confirm=True)
            said.append(reply)
            if done(reply):
                return reply
        raise AssertionError(f"{name}: нужного ответа нет за {rounds} раундов: {said[-3:]}")

    async def raw_status(self, path: str, body: bytes) -> int:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(f"http://127.0.0.1:{self.port}/{path}", data=body) as resp:
                return resp.status

    def secret_in_logs(self, child_lines: list) -> dict:
        """Где встречается секретный путь: файлы `logs/` дома и вывод процессов шлюза."""
        found = {}
        log_dir = self.home / "logs"
        for path in sorted(log_dir.rglob("*")) if log_dir.is_dir() else []:
            if path.is_file():
                found[str(path.relative_to(self.home))] = self.secret in path.read_text(encoding="utf-8", errors="replace")
        found["child-output"] = self.secret in "\n".join(child_lines)
        return found
