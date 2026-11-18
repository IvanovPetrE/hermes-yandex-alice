"""Доставка в Telegram (docs/DESIGN.md, этап 5, срез «а») — только стандартная библиотека и asyncio, без Hermes.

Исход отправки — три значения (S3): `delivered` (`success`), `failed` (`error` от отправителя Hermes — отказ Telegram или
«возможно дошло», различить нельзя; не повторяется), `uncertain` (общий срок, исключение, неожиданный ответ).
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

DELIVERED, FAILED, UNCERTAIN = "delivered", "failed", "uncertain"


def outcome_of(result: object) -> str:
    """Исход по `dict` отправителя (`standalone_sender_fn`)."""
    if isinstance(result, dict) and result.get("success"):
        return DELIVERED
    if isinstance(result, dict) and result.get("error"):
        return FAILED
    return UNCERTAIN


# Ключи `extra`, которые читает отправитель Telegram Hermes v0.23.1 (`plugins/platforms/telegram/standalone.py`), с
# префиксом `telegram_` в настройках плагина — чтобы не пересекаться с его собственными (`port`).
TELEGRAM_EXTRA = {"telegram_base_url": "base_url", "telegram_base_file_url": "base_file_url",
                  "telegram_proxy_url": "proxy_url", "telegram_fallback_ips": "fallback_ips",
                  "telegram_disable_link_previews": "disable_link_previews"}


def telegram_extra(extra: dict) -> dict:
    """`extra` для `PlatformConfig` отправителя: только белый список, пустые значения отброшены."""
    return {dst: extra[src] for src, dst in TELEGRAM_EXTRA.items() if extra.get(src)}


@dataclass(frozen=True)
class Pending:
    """Отправка без исхода. attempts — сколько раз начиналась (пишется до отправки)."""
    id: int
    conversation: str
    text: str                  # уже с подписью «Алиса:»
    fallback: tuple[str, ...]  # части голоса для «Озвучить?» при failed (Д12)
    created_at: float
    attempts: int = 0


MAX_ATTEMPTS = 2   # первая отправка и один повтор после обрыва перезапуском (решение владельца, этап 5)


def recover(rows, now: float, ttl: float, enabled: bool) -> tuple[list[Pending], list[Pending]]:
    """`(отправить, бросить)` для отправок без исхода при старте. Не начатая уходит как обычная; оборванная — один
    повтор; оборванный повтор, старше `ttl` или при выключенном Telegram — бросить (исход uncertain)."""
    send, give_up = [], []
    for row in rows:
        if not enabled or now - row.created_at >= ttl or row.attempts >= MAX_ATTEMPTS:
            give_up.append(row)
        else:
            send.append(row)
    return send, give_up


class Courier:
    """Очередь отправок в Telegram: FIFO, по одной (решение владельца, этап 5), вне бюджета ответа Алисе.

    `send(text)` — корутина отправителя (адаптер запускает её в копии контекста `connect()`, S3 T6); `store` — таблица
    `outbox` хранилища или None (сбой базы: очередь в памяти, без повтора после перезапуска); `report(row, outcome)` —
    исход в машину беседы (`Delivery`)."""

    def __init__(self, send: Callable[[str], Awaitable[object]] | None, store, report: Callable[[Pending, str], None],
                 wall: Callable[[], float] = time.time, deadline: float = 60.0, ttl: float = 86400.0,
                 log: Callable[[str], None] | None = None):
        self._send, self._store, self._report = send, store, report
        self._wall, self._deadline, self._ttl = wall, deadline, ttl
        self._log = log or logger.info
        self._queue: deque[Pending] = deque()
        self._worker: asyncio.Task | None = None
        self._ids = itertools.count(1)   # без хранилища

    def enqueue(self, conversation: str, text: str, fallback: tuple[str, ...] = ()) -> None:
        """Синхронно: запись в `outbox` до возврата — шаг машины, породивший отправку, уже сохранён (engine)."""
        now = self._wall()
        sid = self._write(self._store and self._store.add, conversation, text, tuple(fallback), now)
        if sid is None:   # без базы или запись не удалась: отправка — только в памяти
            sid = -next(self._ids)
        self._queue.append(Pending(id=sid, conversation=conversation, text=text, fallback=tuple(fallback),
                                   created_at=now))
        self._wake()

    def resume(self, enabled: bool) -> None:
        """`connect()`: отправки без исхода, оставшиеся от прошлого экземпляра (перезапуск или переподключение). Правило —
        `recover`; брошенные — исход uncertain, текст стирается; остальные — в очередь в прежнем порядке."""
        if self._store is None:
            return
        rows = self._write(self._store.pending) or []
        queued = {row.id for row in self._queue}
        send, give_up = recover([r for r in rows if r.id not in queued], self._wall(), self._ttl, enabled)
        for row in give_up:
            self._log(f"telegram send given up: attempts={row.attempts} length={len(row.text)}")
            self._finish(row, UNCERTAIN)
        if send and self._send is not None:
            self._queue.extend(send)
            self._wake()

    def _write(self, op, *args):
        """Запись в `outbox`; сбой базы доставку не ломает — в лог только класс ошибки."""
        if not op:
            return None
        try:
            return op(*args)
        except Exception as exc:  # noqa: BLE001
            self._log(f"outbox write failed: {type(exc).__name__}")
            return None

    def _wake(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        while self._queue:
            row = self._queue.popleft()
            self._write(self._store and self._store.started, row.id)   # до отправки: обрыв после — оборванная попытка
            try:
                outcome = outcome_of(await asyncio.wait_for(self._send(row.text), self._deadline))
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():   # close(): обрыв, исхода нет — запись остаётся
                    raise
                outcome = UNCERTAIN
            except Exception as exc:  # noqa: BLE001 - включая TimeoutError срока; текст исключения может нести текст
                self._log(f"telegram send raised: {type(exc).__name__}")
                outcome = UNCERTAIN
            self._log(f"telegram send: outcome={outcome} attempt={row.attempts + 1} length={len(row.text)}")
            self._finish(row, outcome)

    def _finish(self, row: Pending, outcome: str) -> None:
        """Сначала удалить, потом сообщить: обрыв между ними теряет озвучку исхода, а не даёт второй доставки."""
        self._write(self._store and self._store.done, row.id)
        try:
            self._report(row, outcome)
        except Exception as exc:  # noqa: BLE001 - исход не должен остановить очередь
            self._log(f"delivery report failed: {type(exc).__name__}")

    async def close(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
