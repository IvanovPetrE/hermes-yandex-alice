"""Лимит одновременных соединений слушателя (HERMESYAND-6, docs/PLAN.md, этап 3).

Только стандартная библиотека, без aiohttp и Hermes. `ConnectionLimit` — фабрика протоколов для
`loop.create_server`: сверх лимита соединение закрывается сразу после accept, без ответа и до чтения данных.
Счёт ведётся в самой фабрике, а не по списку соединений aiohttp: пачка accept за один проход цикла событий
иначе проскочила бы лимит.
"""
import asyncio
import logging
import time
from typing import Callable

logger = logging.getLogger(__name__)


class _Drop(asyncio.Protocol):
    """Лишнее соединение: закрывается сразу, данные игнорируются."""

    def connection_made(self, transport) -> None:
        transport.abort()


class _Counted(asyncio.Protocol):
    """Посредник вокруг настоящего протокола: освобождает слот при закрытии соединения (один раз)."""

    def __init__(self, inner, release: Callable[[], None]) -> None:
        self._inner, self._release = inner, release

    def connection_made(self, transport) -> None:
        self._inner.connection_made(transport)

    def data_received(self, data: bytes) -> None:
        self._inner.data_received(data)

    def eof_received(self):
        return self._inner.eof_received()

    def pause_writing(self) -> None:
        self._inner.pause_writing()

    def resume_writing(self) -> None:
        self._inner.resume_writing()

    def connection_lost(self, exc) -> None:
        release, self._release = self._release, lambda: None
        try:
            self._inner.connection_lost(exc)
        finally:
            release()


class ConnectionLimit:
    def __init__(self, factory: Callable[[], asyncio.Protocol], limit: int, *,
                 clock: Callable[[], float] = time.monotonic, log_every: float = 60.0) -> None:
        self._factory, self._limit, self.active = factory, limit, 0
        self._clock, self._log_every = clock, log_every
        self._rejected = 0              # отказов с прошлой записи в лог
        self._last_log: float | None = None

    def _note_rejection(self) -> None:
        """Первый отказ — в лог сразу, дальше не чаще раза в интервал, со счётчиком; без адреса клиента и порта."""
        self._rejected += 1
        now = self._clock()
        if self._last_log is None or now - self._last_log >= self._log_every:
            logger.warning("connection limit reached: rejected=%d", self._rejected)
            self._rejected, self._last_log = 0, now

    def __call__(self) -> asyncio.Protocol:
        if self.active >= self._limit:
            self._note_rejection()
            return _Drop()
        self.active += 1
        try:
            inner = self._factory()
        except BaseException:
            self.active -= 1
            raise
        return _Counted(inner, self._release)

    def _release(self) -> None:
        self.active -= 1
