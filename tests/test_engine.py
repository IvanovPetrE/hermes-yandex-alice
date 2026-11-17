"""Исполнитель машины беседы (docs/PLAN.md, этап 3, срез «б», б2) — без Hermes, aiohttp и сети.

Шлюз и чтение истории — фейки; бюджет ожидания в тестах — доли секунды.
"""
import asyncio
import itertools
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "yandex-alice"))
import dialog as d  # noqa: E402
import engine as en  # noqa: E402
import protocol as p  # noqa: E402

SETTINGS = d.Settings(telegram=False)


def rq(text="вопрос", mid="1", new=False, nlu=None, session="sess-1") -> p.AliceRequest:
    return p.AliceRequest(text=text, skill_id="skill-1", user_id="u-owner", session_id=session, message_id=mid,
                          new=new, nlu=nlu)


class Harness:
    """Движок с фейковым шлюзом: `asks` — что ушло в Hermes; `recall_result` — что вернёт чтение истории."""

    def __init__(self, **kw):
        self.asks = []
        self.recall_calls = 0
        self.recall_result = ("found", "Старый ответ")
        self.recall_delay = 0.0
        self.ask_error = None
        self.ids = (f"c{i}" for i in itertools.count(1))
        self.clock = 1000.0
        self.lines = []

        async def ask(conversation, text):
            if self.ask_error:
                raise self.ask_error
            self.asks.append((conversation, text))

        async def recall(conversation):
            self.recall_calls += 1
            await asyncio.sleep(self.recall_delay)
            return self.recall_result

        opts = dict(budget=0.2, recall_timeout=0.1)
        opts.update(kw)
        self.engine = en.Engine(SETTINGS, ask, recall, lambda: next(self.ids), wall=lambda: self.clock,
                                log=self.lines.append, **opts)

    @property
    def conv(self) -> str:
        return self.engine.state.conversation


async def settle() -> None:
    await asyncio.sleep(0.02)


class AskTests(unittest.IsolatedAsyncioTestCase):
    async def test_question_goes_to_the_gateway_and_the_final_is_the_reply(self) -> None:
        h = Harness()
        task = asyncio.create_task(h.engine.handle(rq("вопрос")))
        await settle()
        self.assertEqual(h.asks, [(h.conv, "вопрос")])
        h.engine.on_turn_start(h.conv, "m1", False)
        h.engine.on_final(h.conv, "Ответ", False)
        h.engine.on_turn_complete(h.conv, "m1", False)
        self.assertEqual(await task, "Ответ")

    async def test_a_final_after_the_budget_becomes_the_unclaimed_answer(self) -> None:
        h = Harness(budget=0.05)
        self.assertEqual(await h.engine.handle(rq("вопрос")), p.REPLY_ACCEPTED)
        h.engine.on_turn_start(h.conv, "m1", False)
        h.engine.on_final(h.conv, "Поздний ответ", False)
        self.assertEqual(h.engine.state.answer.parts, ("Поздний ответ",))

    async def test_a_retry_during_the_hold_gets_the_same_answer(self) -> None:
        h = Harness(budget=1.0)
        first = asyncio.create_task(h.engine.handle(rq("вопрос", mid="7")))
        await settle()
        retry = asyncio.create_task(h.engine.handle(rq("вопрос", mid="7")))
        await settle()
        h.engine.on_final(h.conv, "Ответ", False)
        self.assertEqual((await first, await retry), ("Ответ", "Ответ"))
        self.assertEqual(len(h.asks), 1)   # агент спрошен один раз

    async def test_a_repeated_turn_gets_the_previous_reply_without_a_second_ask(self) -> None:
        h = Harness()
        task = asyncio.create_task(h.engine.handle(rq("вопрос", mid="9")))
        await settle()
        h.engine.on_final(h.conv, "Ответ", False)
        self.assertEqual(await task, "Ответ")
        self.assertEqual(await h.engine.handle(rq("вопрос", mid="9")), "Ответ")
        self.assertEqual(len(h.asks), 1)


class TurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_turn_closed_without_a_final_fails_the_waiting_question(self) -> None:
        h = Harness(budget=1.0)
        task = asyncio.create_task(h.engine.handle(rq("вопрос")))
        await settle()
        h.engine.on_turn_start(h.conv, "m1", False)
        h.engine.on_turn_complete(h.conv, "m1", False)   # исключение или пустой ход, итога не было
        self.assertEqual(await task, p.REPLY_FAILED)
        self.assertIsNone(h.engine.state.question)

    async def test_a_turn_closed_after_the_final_keeps_the_answer(self) -> None:
        h = Harness(budget=0.05)
        await h.engine.handle(rq("вопрос"))
        h.engine.on_turn_start(h.conv, "m1", False)
        h.engine.on_final(h.conv, "Ответ", False)
        h.engine.on_turn_complete(h.conv, "m1", False)
        self.assertEqual(h.engine.state.answer.parts, ("Ответ",))
        self.assertEqual(h.engine.state.announce, "")   # «не получилось» не прозвучит

    async def test_an_empty_internal_turn_does_not_close_the_question(self) -> None:
        # S5, вывод 6: после перезапуска Hermes дозапускает внутренний ход; первый пустой, вопрос не закрывается.
        h = Harness(budget=0.05)
        await h.engine.handle(rq("вопрос"))
        h.engine.on_gateway_started()
        h.engine.on_turn_start(h.conv, None, True)
        self.assertTrue(h.engine.state.question.internal_seen)
        h.engine.on_turn_complete(h.conv, None, True)
        self.assertIsNotNone(h.engine.state.question)

    async def test_a_question_lost_by_a_restart_is_announced_after_lost_after(self) -> None:
        h = Harness(budget=0.05)
        await h.engine.handle(rq("вопрос"))
        h.engine.on_gateway_started()
        self.assertEqual(h.engine.state.question.restarted_at, 1000.0)
        h.clock = 1000.0 + SETTINGS.lost_after
        h.engine.tick()
        self.assertIsNone(h.engine.state.question)
        self.assertEqual(h.engine.state.announce, p.REPLY_LOST)

    async def test_an_old_unclaimed_answer_is_dropped_on_tick_and_ready_at_is_wall_time(self) -> None:
        h = Harness(budget=0.05)
        await h.engine.handle(rq("вопрос"))
        h.clock = 2000.0
        h.engine.on_final(h.conv, "Ответ", False)
        self.assertEqual(h.engine.state.answer.ready_at, 2000.0)
        h.clock = 2000.0 + SETTINGS.unclaimed_ttl
        h.engine.tick()
        self.assertIsNone(h.engine.state.answer)   # Telegram выключен: ответ удалён, отправки нет (принято владельцем)


class RobustnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_unexpected_error_is_a_polite_reply_and_logs_only_the_class(self) -> None:
        h = Harness()

        def boom() -> float:
            raise ValueError("секретный текст")

        h.engine._wall = boom
        self.assertEqual(await h.engine.handle(rq("вопрос")), p.REPLY_ERROR)
        self.assertTrue(any("ValueError" in line for line in h.lines))
        self.assertFalse(any("секретный" in line for line in h.lines))

    async def test_close_releases_waiting_requests_and_cancels_tasks(self) -> None:
        h = Harness(budget=5.0)
        h.engine.on_turn_start(h.conv, None, True)   # гейт держит задачу Ask
        task = asyncio.create_task(h.engine.handle(rq("вопрос")))
        await settle()
        await h.engine.close()
        self.assertEqual(await task, p.REPLY_ERROR)
        self.assertEqual(h.asks, [])
        self.assertEqual(h.engine._tasks, set())


REPEAT = {"intents": {"YANDEX.REPEAT": {}}}


class RecallTests(unittest.IsolatedAsyncioTestCase):
    """«Повтори» в обычной беседе — последний ответ из истории Hermes, без хода агента (S7)."""

    async def test_the_last_answer_is_read_from_history_and_spoken(self) -> None:
        h = Harness(budget=1.0)
        self.assertEqual(await h.engine.handle(rq("повтори", nlu=REPEAT)), "Старый ответ")
        self.assertEqual(h.recall_calls, 1)
        self.assertEqual(h.asks, [])   # агент не спрошен

    async def test_a_read_that_misses_the_deadline_is_a_failure(self) -> None:
        h = Harness(budget=1.0, recall_timeout=0.05)
        h.recall_delay = 0.5
        self.assertEqual(await h.engine.handle(rq("повтори", nlu=REPEAT)), p.REPLY_FAILED)

    async def test_a_busy_or_failing_read_is_a_failure(self) -> None:
        h = Harness(budget=1.0)
        h.recall_result = ("error", None)   # адаптер: замок занят или исключение в рабочем потоке
        self.assertEqual(await h.engine.handle(rq("повтори", nlu=REPEAT)), p.REPLY_FAILED)

    async def test_a_raising_read_is_a_failure(self) -> None:
        h = Harness(budget=1.0)

        async def boom(conversation):
            raise RuntimeError("locked")

        h.engine._recall = boom
        self.assertEqual(await h.engine.handle(rq("повтори", nlu=REPEAT)), p.REPLY_FAILED)

    async def test_a_retry_during_the_read_shares_one_read(self) -> None:
        h = Harness(budget=1.0, recall_timeout=0.5)
        h.recall_delay = 0.1
        first = asyncio.create_task(h.engine.handle(rq("повтори", mid="5", nlu=REPEAT)))
        await settle()
        retry = asyncio.create_task(h.engine.handle(rq("повтори", mid="5", nlu=REPEAT)))
        self.assertEqual((await first, await retry), ("Старый ответ", "Старый ответ"))
        self.assertEqual(h.recall_calls, 1)


class GateTests(unittest.IsolatedAsyncioTestCase):
    """Гейт Р2 (S1, вывод 2): пока ход беседы открыт в Hermes, новая фраза шлюзу не передаётся."""

    async def test_the_phrase_waits_for_the_open_turn_to_close(self) -> None:
        h = Harness(budget=1.0, gate_timeout=0.5)
        h.engine.on_turn_start(h.conv, None, True)   # внутренний ход без вопроса в работе (cron, фон)
        task = asyncio.create_task(h.engine.handle(rq("вопрос")))
        await settle()
        self.assertEqual(h.asks, [])
        h.engine.on_turn_complete(h.conv, None, True)
        await settle()
        self.assertEqual(h.asks, [(h.conv, "вопрос")])
        h.engine.on_final(h.conv, "Ответ", False)
        self.assertEqual(await task, "Ответ")

    async def test_a_turn_that_never_closes_fails_the_question_and_leaves_none_in_work(self) -> None:
        h = Harness(budget=1.0, gate_timeout=0.05)
        h.engine.on_turn_start(h.conv, None, True)
        self.assertEqual(await h.engine.handle(rq("вопрос")), p.REPLY_FAILED)
        self.assertEqual(h.asks, [])
        self.assertIsNone(h.engine.state.question)

    async def test_a_failing_ask_fails_the_question_and_logs_only_the_class(self) -> None:
        h = Harness(budget=1.0)
        h.ask_error = RuntimeError("секретный текст вопроса")
        self.assertEqual(await h.engine.handle(rq("вопрос")), p.REPLY_FAILED)
        self.assertIsNone(h.engine.state.question)
        self.assertTrue(any("RuntimeError" in line for line in h.lines))
        self.assertFalse(any("секретный" in line or "вопрос" in line for line in h.lines))


if __name__ == "__main__":
    unittest.main()
