"""Доставка в Telegram (docs/PLAN.md, этап 5, срез «а») — без Hermes и сети: отправитель — фейк, SQLite — настоящий."""
import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "yandex-alice"))
import delivery  # noqa: E402
import storage  # noqa: E402


class OutcomeTests(unittest.TestCase):
    """Ответ отправителя Hermes (`standalone_sender_fn`) — `dict`; флага «возможно дошло» в нём нет (S3)."""

    def test_the_sender_result_maps_to_three_outcomes(self) -> None:
        cases = {
            "success": ({"success": True, "platform": "telegram", "chat_id": "100", "message_id": 7}, "delivered"),
            "telegram error": ({"error": "Telegram send failed: Unauthorized"}, "failed"),
            "nothing": (None, "uncertain"),
            "not a dict": ("ok", "uncertain"),
            "empty dict": ({}, "uncertain"),
        }
        for name, (result, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(delivery.outcome_of(result), expected)


class ExtraTests(unittest.TestCase):
    """Отправителю — только ключи Telegram из `platforms.yandex_alice.extra`, без префикса (S3, вывод 1)."""

    def test_only_known_telegram_keys_pass_without_the_prefix(self) -> None:
        extra = {"port": 8443, "telegram_base_url": "http://127.0.0.1:1/bot", "telegram_proxy_url": "",
                 "telegram_fallback_ips": ["ip-1"], "telegram_unknown": "x", "base_url": "http://example.test/bot",
                 "telegram_disable_link_previews": True}
        self.assertEqual(delivery.telegram_extra(extra),
                         {"base_url": "http://127.0.0.1:1/bot", "fallback_ips": ["ip-1"],
                          "disable_link_previews": True})


DAY = 86400.0


def pending(id_, attempts, created_at=1000.0, fallback=()):
    return delivery.Pending(id=id_, conversation="c1", text=f"Алиса:\nтекст {id_}", fallback=fallback,
                            created_at=created_at, attempts=attempts)


class RecoverTests(unittest.TestCase):
    """Отправки без исхода при старте (решение владельца, этап 5): не начатая — как обычная, оборванная — один повтор,
    оборванный повтор, старше суток или Telegram выключен — не отправляется (uncertain)."""

    def test_what_is_sent_again_and_what_is_given_up(self) -> None:
        rows = [pending(1, 0), pending(2, 1), pending(3, 2), pending(4, 1, created_at=1000.0 - DAY)]
        send, give_up = delivery.recover(rows, now=1000.0, ttl=DAY, enabled=True)
        self.assertEqual(([r.id for r in send], [r.id for r in give_up]), ([1, 2], [3, 4]))

    def test_without_telegram_everything_is_given_up(self) -> None:
        send, give_up = delivery.recover([pending(1, 0), pending(2, 1)], now=1000.0, ttl=DAY, enabled=False)
        self.assertEqual(([r.id for r in send], [r.id for r in give_up]), ([], [1, 2]))


class FakeTelegram:
    """Отправитель: `result` — что вернуть, `hang` — зависнуть до отмены, `error` — бросить."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.result: object = {"success": True, "message_id": 1}
        self.hang = False
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def __call__(self, text: str) -> object:
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        if self.hang:
            await asyncio.sleep(3600)
        if self.gate is not None:
            await self.gate.wait()
        return self.result


async def until(cond, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


class CourierCase(unittest.IsolatedAsyncioTestCase):
    """Курьер с фейковым отправителем и настоящим SQLite; исходы — в `reports`, лог — в `lines`."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "state.db"
        self.tg = FakeTelegram()
        self.reports: list[tuple[str, str, tuple[str, ...], str]] = []
        self.lines: list[str] = []
        self.clock = 1000.0

    def store(self) -> "storage.Store":
        st = storage.Store(sqlite3.connect(self.path))
        self.addCleanup(st.close)
        return st

    def courier(self, store="new", deadline: float = 1.0) -> "delivery.Courier":
        return delivery.Courier(self.tg, self.store() if store == "new" else store,
                                lambda row, outcome: self.reports.append((row.conversation, row.text, row.fallback,
                                                                          outcome)),
                                wall=lambda: self.clock, deadline=deadline, log=self.lines.append)


class CourierTests(CourierCase):
    async def test_a_delivered_send_is_reported_and_forgotten(self) -> None:
        c = self.courier()
        c.enqueue("c1", "Алиса:\nподробно", ("Часть.",))
        await until(lambda: self.reports)
        self.assertEqual(self.reports, [("c1", "Алиса:\nподробно", ("Часть.",), "delivered")])
        self.assertEqual(self.tg.calls, ["Алиса:\nподробно"])
        self.assertEqual(self.store().pending(), [])
        await c.close()

    async def test_a_telegram_error_is_failed_and_not_repeated(self) -> None:
        self.tg.result = {"error": "Telegram send failed: Forbidden"}
        c = self.courier()
        c.enqueue("c1", "Алиса:\nподробно", ("Часть.",))
        await until(lambda: self.reports)
        await asyncio.sleep(0.05)
        self.assertEqual([r[3] for r in self.reports], ["failed"])
        self.assertEqual(len(self.tg.calls), 1)
        self.assertEqual(self.store().pending(), [])
        await c.close()

    async def test_a_hanging_send_is_uncertain_after_the_deadline_and_not_repeated(self) -> None:
        self.tg.hang = True
        c = self.courier(deadline=0.05)
        c.enqueue("c1", "Алиса:\nподробно")
        await until(lambda: self.reports)
        await asyncio.sleep(0.1)
        self.assertEqual([r[3] for r in self.reports], ["uncertain"])
        self.assertEqual(len(self.tg.calls), 1)
        self.assertEqual(self.store().pending(), [])
        await c.close()

    async def test_a_raising_sender_is_uncertain_and_logs_only_the_class(self) -> None:
        self.tg.error = RuntimeError("Алиса:\nсекретный текст")
        c = self.courier()
        c.enqueue("c1", "Алиса:\nсекретный текст")
        await until(lambda: self.reports)
        self.assertEqual([r[3] for r in self.reports], ["uncertain"])
        self.assertTrue(any("RuntimeError" in line for line in self.lines))
        self.assertFalse(any("секретный" in line for line in self.lines))
        await c.close()

    async def test_sends_go_one_at_a_time_in_order(self) -> None:
        self.tg.gate = asyncio.Event()
        c = self.courier()
        c.enqueue("c1", "раз")
        c.enqueue("c1", "два")
        await until(lambda: self.tg.calls)
        await asyncio.sleep(0.05)
        self.assertEqual(self.tg.calls, ["раз"])   # вторая ждёт исхода первой
        self.tg.gate.set()
        await until(lambda: len(self.reports) == 2)
        self.assertEqual([r[1] for r in self.reports], ["раз", "два"])
        await c.close()


class RestartTests(CourierCase):
    """Обрыв перезапуском или переподключением (`close()` посреди отправки) — один повтор на следующем старте."""

    async def interrupted(self) -> None:
        """Отправка оборвана посреди попытки: `close()` курьера, как в `disconnect()` адаптера."""
        self.tg.hang = True
        c = self.courier()
        c.enqueue("c1", "Алиса:\nподробно", ("Часть.",))
        await until(lambda: self.tg.calls)
        await c.close()

    async def test_a_send_cut_by_a_restart_is_sent_once_more(self) -> None:
        await self.interrupted()
        self.assertEqual([r.attempts for r in self.store().pending()], [1])
        self.assertEqual(self.reports, [])   # обрыв — не исход
        self.tg.hang = False
        c = self.courier()
        c.resume(enabled=True)
        await until(lambda: self.reports)
        self.assertEqual(self.reports, [("c1", "Алиса:\nподробно", ("Часть.",), "delivered")])
        self.assertEqual(len(self.tg.calls), 2)
        await c.close()

    async def test_a_cut_retry_is_uncertain_and_not_sent_again(self) -> None:
        await self.interrupted()
        second = self.courier()
        second.resume(enabled=True)
        await until(lambda: len(self.tg.calls) == 2)
        await second.close()
        third = self.courier()
        third.resume(enabled=True)
        await until(lambda: self.reports)
        self.assertEqual([r[3] for r in self.reports], ["uncertain"])
        self.assertEqual(len(self.tg.calls), 2)
        self.assertEqual(self.store().pending(), [])
        await third.close()

    async def test_a_stale_send_or_a_disabled_telegram_gives_up_without_sending(self) -> None:
        store = self.store()
        store.add("c1", "старая", (), self.clock - DAY)
        c = self.courier(store)
        c.resume(enabled=True)
        store.add("c1", "свежая", (), self.clock)
        c.resume(enabled=False)
        await asyncio.sleep(0.05)
        self.assertEqual([(r[1], r[3]) for r in self.reports], [("старая", "uncertain"), ("свежая", "uncertain")])
        self.assertEqual(self.tg.calls, [])
        self.assertEqual(store.pending(), [])
        await c.close()

    async def test_sends_never_started_go_out_in_order_after_a_restart(self) -> None:
        store = self.store()
        store.add("c1", "раз", (), self.clock)
        store.add("c1", "два", (), self.clock)
        c = self.courier(store)
        c.resume(enabled=True)
        await until(lambda: len(self.reports) == 2)
        self.assertEqual([(r[1], r[3]) for r in self.reports], [("раз", "delivered"), ("два", "delivered")])
        await c.close()

    async def test_without_storage_sends_still_go_out(self) -> None:
        c = self.courier(store=None)
        c.enqueue("c1", "раз")
        await until(lambda: self.reports)
        self.assertEqual([r[3] for r in self.reports], ["delivered"])
        await c.close()


if __name__ == "__main__":
    unittest.main()
