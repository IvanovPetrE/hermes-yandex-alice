"""Лимит одновременных соединений (HERMESYAND-6, docs/DESIGN.md, этап 3) — без Hermes и aiohttp: настоящий loopback,
протокол-эхо вместо aiohttp. Срез 1: соединение сверх лимита закрывается сразу, принятые работают."""
import asyncio
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "yandex-alice"))
import listener  # noqa: E402


class Echo(asyncio.Protocol):
    def connection_made(self, transport) -> None:
        self.transport = transport

    def data_received(self, data: bytes) -> None:
        self.transport.write(data)


class LimitTests(unittest.IsolatedAsyncioTestCase):
    async def serve(self, limit: int):
        quiet = logging.NullHandler()   # отказы пишутся в лог; здесь их не проверяем и вывод не засоряем
        logging.getLogger("listener").addHandler(quiet)
        self.addCleanup(logging.getLogger("listener").removeHandler, quiet)
        loop = asyncio.get_running_loop()
        server = await loop.create_server(listener.ConnectionLimit(Echo, limit), "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        return server.sockets[0].getsockname()[1]

    async def test_connection_over_the_limit_is_closed_at_once(self) -> None:
        port = await self.serve(2)
        accepted = []
        for _ in range(2):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            self.addCleanup(writer.close)
            accepted.append((reader, writer))
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.addCleanup(writer.close)
        try:   # лишнее соединение закрыто сервером без ответа: чтение даёт конец потока (или сброс)
            data = await asyncio.wait_for(reader.read(1), 2)
        except ConnectionResetError:
            data = b""
        self.assertEqual(data, b"")
        for reader, writer in accepted:   # принятые продолжают работать
            writer.write(b"ping")
            self.assertEqual(await asyncio.wait_for(reader.readexactly(4), 2), b"ping")

    async def test_slot_is_freed_when_an_accepted_connection_closes(self) -> None:
        port = await self.serve(1)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()
        for _ in range(50):   # сервер узнаёт о закрытии асинхронно
            reader2, writer2 = await asyncio.open_connection("127.0.0.1", port)
            self.addCleanup(writer2.close)
            writer2.write(b"ok")
            try:
                if await asyncio.wait_for(reader2.read(2), 1) == b"ok":
                    return
            except ConnectionResetError:
                pass
            await asyncio.sleep(0.02)
        self.fail("слот не освободился после закрытия соединения")


class SlotTests(unittest.TestCase):
    def test_failing_inner_connection_lost_still_frees_the_slot(self) -> None:
        class Broken(asyncio.Protocol):
            def connection_lost(self, exc) -> None:
                raise RuntimeError("boom")

        factory = listener.ConnectionLimit(Broken, 1)
        proto = factory()
        self.assertEqual(factory.active, 1)
        with self.assertRaises(RuntimeError):
            proto.connection_lost(None)
        self.assertEqual(factory.active, 0)
        with self.assertRaises(RuntimeError):   # повторный connection_lost слот второй раз не освобождает
            proto.connection_lost(None)
        self.assertEqual(factory.active, 0)

    def test_failing_inner_factory_does_not_take_a_slot(self) -> None:
        def broken():
            raise RuntimeError("boom")

        factory = listener.ConnectionLimit(broken, 1)
        with self.assertRaises(RuntimeError):
            factory()
        self.assertEqual(factory.active, 0)


class _Transport:
    def abort(self) -> None:
        pass


class RejectLogTests(unittest.TestCase):
    """Отказы — в лог одной записью за интервал со счётчиком; без адреса клиента, порта и пути."""

    def make(self, now):
        return listener.ConnectionLimit(Echo, 1, clock=lambda: now[0], log_every=60.0)

    def reject(self, limit_factory):
        limit_factory().connection_made(_Transport())

    def test_first_rejection_is_logged_then_one_record_per_interval(self) -> None:
        now = [1000.0]
        factory = self.make(now)
        factory()   # занял единственный слот
        with self.assertLogs("listener", level="WARNING") as logs:
            self.reject(factory)
            now[0] += 10
            self.reject(factory)
            self.reject(factory)
            now[0] += 60
            self.reject(factory)
        self.assertEqual([r.getMessage() for r in logs.records],
                         ["connection limit reached: rejected=1", "connection limit reached: rejected=3"])


if __name__ == "__main__":
    unittest.main()
