"""Шлюз Hermes с нашим плагином как дочерний процесс теста совместимости (этап 6, срез «в»; S5).

Запуск — только из `restart.py` (`python -I -u gateway_child.py <checkout>`), окружение — то, что собрал `run.sh` (`env -i`,
временные HOME/HERMES_HOME). Родитель шлёт команды JSON-строками в stdin, дочерний отвечает событиями `HYA {...}` в stdout;
вызовы шлюза к адаптеру пишет в `<HERMES_HOME>/compat-journal.jsonl` (с pid). EOF на stdin — выход: если родителя убил
`timeout`, шлюз не остаётся. Тестовая обвязка Hermes (`gateway.run`, приватные атрибуты) разрешена только в `compat/`.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent


def emit(**kw) -> None:
    print("HYA " + json.dumps(kw, ensure_ascii=False, default=str), flush=True)


class FileJournal:
    """Список для `harness.instrument`: каждая запись — строка JSONL (общая для всех запусков шлюза в доме)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, op) -> None:
        row = asdict(op)
        row["pid"] = os.getpid()
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


async def main(checkout: str) -> None:
    import asyncio

    sys.path.insert(0, checkout)
    sys._hermes_pin_default_home = True
    import hermes_bootstrap  # noqa: F401  (рантайм-пакеты Hermes)
    sys.path.insert(0, str(HERE))
    import harness
    from gateway.config import Platform, load_gateway_config
    from gateway.run import GatewayRunner
    from gateway.run_plugin_rewire import reload_plugins_verb
    from hermes_logging import setup_logging

    home = harness.temp_home()
    journal = FileJournal(home / "compat-journal.jsonl")
    hold_flag = home / "hold_final"
    setup_logging(mode="gateway")   # как `hermes gateway run`: без файловых логов проверка «секрета нет в логах» была бы пустой
    runner = GatewayRunner(load_gateway_config())
    loop = asyncio.get_running_loop()

    def alice():
        return runner.adapters.get(Platform(harness.PLATFORM))

    def name_of(key) -> str:
        return str(getattr(key, "value", key))

    def failed() -> dict:
        return {name_of(k): {"attempts": v.get("attempts"), "paused": bool(v.get("paused"))}
                for k, v in (getattr(runner, "_failed_platforms", None) or {}).items()}

    def hold_final(adapter) -> None:
        """Пока есть флаг `hold_final`, первая итоговая отправка зависает внутри `send()` (до SIGKILL родителем)."""
        orig = adapter.send

        async def send(chat_id, content, reply_to=None, metadata=None):
            meta = metadata or {}
            if hold_flag.exists() and meta.get("notify") is True and not meta.get("_interim_send"):
                journal.append(harness.Op(time.monotonic(), "held", str(chat_id), content=content or ""))
                await asyncio.sleep(3600)
            return await orig(chat_id, content, reply_to, metadata)
        adapter.send = send

    def hold_pending() -> None:
        """Пока есть флаг `hold_pending`, итоговая запись леджера зависает между `pending` и `attempting` (до SIGKILL
        родителем): в БД остаётся строка `pending` — Hermes досылает её без пометки (HERMESYAND-8). Естественным сбоем
        это окно не поймать: оно — один `to_thread` между двумя записями."""
        import gateway.delivery_ledger as ledger

        orig = ledger.mark_attempting
        flag = home / "hold_pending"

        def mark_attempting(obligation_id):
            if flag.exists():
                journal.append(harness.Op(time.monotonic(), "held_pending", "", content=str(obligation_id)))
                time.sleep(3600)
            return orig(obligation_id)
        ledger.mark_attempting = mark_attempting

    hold_pending()
    seen = {"id": None}

    def attach() -> None:
        a = alice()
        if a is not None and id(a) != seen["id"]:
            seen["id"] = id(a)
            hold_final(a)
            harness.instrument(a, journal)
            journal.append(harness.Op(time.monotonic(), "adapter", "", content=str(id(a))))

    async def watch() -> None:
        while True:
            attach()
            await asyncio.sleep(0.01)

    # Наблюдатель — до `start()`: досылку леджера Hermes делает во время старта, сразу после подключения адаптера.
    watcher = asyncio.ensure_future(watch())
    ok = await runner.start()
    attach()
    emit(ev="started", ok=bool(ok), alice=alice() is not None, failed=failed())

    async def handle(cmd: dict) -> dict:
        op = cmd["op"]
        if op == "info":
            a = alice()
            return {"alice_id": id(a) if a else None, "failed": failed()}
        if op == "reload":
            result = await loop.run_in_executor(None, reload_plugins_verb(runner, loop), {})
            return {"result": {k: v for k, v in result.items() if k in ("reloaded", "adapters_rewired", "error")}}
        if op == "fatal":
            a = alice()
            a._set_fatal_error("compat_forced", "forced by the compat test", retryable=True)
            await a._notify_fatal_error()
            return {}
        if op == "arm_boom":   # исключение в обработчике шлюза на фразе с [boom] (R6 пробы S5)
            a = alice()
            inner = a._message_handler

            async def boom(event):
                if "[boom]" in (event.text or ""):
                    raise RuntimeError("compat boom")
                return await inner(event)
            a.set_message_handler(boom)
            return {}
        if op == "stop":
            watcher.cancel()
            await runner.stop()
            return {}
        raise ValueError(f"unknown op {op}")

    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:   # родитель исчез — не оставаться и не занимать память
            os._exit(1)
        cmd = json.loads(line)
        try:
            out = await handle(cmd)
            emit(ev="resp", id=cmd["id"], ok=True, **out)
        except Exception as exc:  # noqa: BLE001
            emit(ev="resp", id=cmd["id"], ok=False, err=f"{type(exc).__name__}: {exc}")
        if cmd["op"] == "stop":
            os._exit(0)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main(sys.argv[1]))
