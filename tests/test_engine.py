"""Исполнитель машины беседы (docs/DESIGN.md, этап 3, срез «б», б2) — без Hermes, aiohttp и сети.

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
        self.ask_ids = []
        self.on_ask = None
        self.recall_calls = 0
        self.recall_result = ("found", "Старый ответ")
        self.recall_delay = 0.0
        self.ask_error = None
        self.ids = (f"c{i}" for i in itertools.count(1))
        self.clock = 1000.0
        self.lines = []

        async def ask(conversation, text, mid):
            if self.ask_error:
                raise self.ask_error
            self.asks.append((conversation, text))
            self.ask_ids.append(mid)
            if self.on_ask is not None:
                await self.on_ask(text)

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
        h.engine.on_turn_start(h.conv, h.ask_ids[0], False)
        h.engine.on_final(h.conv, "Ответ", False)
        h.engine.on_turn_complete(h.conv, h.ask_ids[0], False)
        self.assertEqual(await task, "Ответ")

    async def test_a_final_after_the_budget_becomes_the_unclaimed_answer(self) -> None:
        h = Harness(budget=0.05)
        self.assertEqual(await h.engine.handle(rq("вопрос")), p.REPLY_ACCEPTED)
        h.engine.on_turn_start(h.conv, h.ask_ids[0], False)
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


class AskedMidTests(unittest.IsolatedAsyncioTestCase):
    """HERMESYAND-26: id фразы текущего вопроса без хука start — для итога перенаправленного хода."""

    async def test_the_asked_phrase_is_known_while_its_question_is_in_work(self) -> None:
        h = Harness(budget=0.05)
        await h.engine.handle(rq("вопрос"))
        self.assertEqual(h.engine.asked_mid(h.conv), h.ask_ids[0])
        self.assertIsNone(h.engine.asked_mid("другая"))
        h.engine.on_final(h.conv, "Ответ", False)
        self.assertIsNone(h.engine.asked_mid(h.conv))   # вопрос закрыт: повтор итога — вне хода

    async def test_a_started_turn_is_not_an_asked_phrase(self) -> None:
        h = Harness(budget=0.05)
        await h.engine.handle(rq("вопрос"))
        h.engine.on_turn_start(h.conv, h.ask_ids[0], False)
        self.assertIsNone(h.engine.asked_mid(h.conv))

    async def test_a_new_question_replaces_the_asked_phrase(self) -> None:
        h = Harness(budget=0.05)
        await h.engine.handle(rq("вопрос", mid="1"))
        h.engine.on_final(h.conv, "Ответ", False)
        await h.engine.handle(rq("да", mid="2", nlu={"intents": {"YANDEX.CONFIRM": {}}}))   # забрать ответ
        await h.engine.handle(rq("второй вопрос", mid="3"))
        self.assertEqual(len(h.ask_ids), 2)
        self.assertEqual(h.engine.asked_mid(h.conv), h.ask_ids[1])


class TurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_turn_closed_without_a_final_fails_the_waiting_question(self) -> None:
        h = Harness(budget=1.0)
        task = asyncio.create_task(h.engine.handle(rq("вопрос")))
        await settle()
        h.engine.on_turn_start(h.conv, h.ask_ids[0], False)
        h.engine.on_turn_complete(h.conv, h.ask_ids[0], False)   # исключение или пустой ход, итога не было
        self.assertEqual(await task, p.REPLY_FAILED)
        self.assertIsNone(h.engine.state.question)

    async def test_a_turn_closed_after_the_final_keeps_the_answer(self) -> None:
        h = Harness(budget=0.05)
        await h.engine.handle(rq("вопрос"))
        h.engine.on_turn_start(h.conv, h.ask_ids[0], False)
        h.engine.on_final(h.conv, "Ответ", False)
        h.engine.on_turn_complete(h.conv, h.ask_ids[0], False)
        self.assertEqual(h.engine.state.answer.parts, ("Ответ",))
        self.assertEqual(h.engine.state.announce, "")   # «не получилось» не прозвучит

    async def test_a_turn_interrupted_by_the_shutdown_keeps_the_question(self) -> None:
        # compat (группа R): штатная остановка шлюза завершает ход без итога; Hermes дозапустит его после старта, как
        # после SIGKILL — вопрос не закрывается, ответ прозвучит голосом
        saved = []
        h = Harness(budget=0.05, save=saved.append)
        await h.engine.handle(rq("вопрос"))
        h.engine.on_turn_start(h.conv, h.ask_ids[0], False)
        h.engine.on_turn_complete(h.conv, h.ask_ids[0], False, interrupted=True)
        self.assertIsNotNone(h.engine.state.question)
        self.assertEqual(h.engine.state.announce, "")
        self.assertIsNotNone(saved[-1].question)

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
        h.clock = 1800.0   # меньше срока потерянного хода от Ask (HERMESYAND-24)
        h.engine.on_final(h.conv, "Ответ", False)
        self.assertEqual(h.engine.state.answer.ready_at, 1800.0)
        h.clock = 1800.0 + SETTINGS.unclaimed_ttl
        h.engine.tick()
        self.assertIsNone(h.engine.state.answer)   # Telegram выключен: ответ удалён, отправки нет (принято владельцем)


def unclaimed(conversation="saved-1") -> d.State:
    """Состояние из хранилища: незабранный ответ прошлой беседы."""
    return d.State(conversation=conversation, last_owner_at=900.0,
                   answer=d.Answer(parts=("Ответ.",), telegram="", whole="Алиса:\nОтвет.", ready_at=950.0))


class StoredStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_loaded_state_is_continued(self) -> None:
        h = Harness(state=unclaimed())
        self.assertEqual(h.conv, "saved-1")
        self.assertEqual(await h.engine.handle(rq("", new=True)), p.REPLY_READY)

    async def test_every_change_is_saved_and_an_idle_tick_is_not(self) -> None:
        saved = []
        h = Harness(save=saved.append)
        h.engine.tick()
        self.assertEqual(saved, [])
        await h.engine.handle(rq("вопрос"))
        self.assertEqual(saved[-1], h.engine.state)
        self.assertIsNotNone(saved[-1].question)

    async def test_failing_storage_does_not_break_the_dialog_and_is_logged_once(self) -> None:
        def broken(state):
            raise OSError("disk full: Секретный ответ")

        h = Harness(save=broken)
        task = asyncio.create_task(h.engine.handle(rq("вопрос")))
        await settle()
        h.engine.on_turn_start(h.conv, h.ask_ids[0], False)
        h.engine.on_final(h.conv, "Ответ", False)
        self.assertEqual(await task, "Ответ")
        self.assertEqual(h.lines, ["state save failed: OSError"])

    async def test_question_in_work_is_not_asked_again_after_a_restart(self) -> None:
        h = Harness(state=d.State(conversation="saved-1", question=d.Question(asked_at=990.0), last_owner_at=990.0))
        h.engine.on_gateway_started()
        await settle()
        self.assertEqual(h.asks, [])
        self.assertIsNotNone(h.engine.state.question)

    async def test_answer_resent_after_a_restart_waits_for_the_owner(self) -> None:
        saved = []
        h = Harness(state=d.State(conversation="saved-1", question=d.Question(asked_at=990.0), last_owner_at=990.0),
                    save=saved.append)
        h.engine.on_gateway_started()
        h.engine.on_turn_start("saved-1", None, True)       # Hermes дозапустил ход (S5)
        h.engine.on_final("saved-1", "Досланный ответ.", True)
        h.engine.on_turn_complete("saved-1", None, True)
        self.assertEqual(saved[-1].answer.parts, ("Досланный ответ.",))
        self.assertEqual(await h.engine.handle(rq("", new=True)), p.REPLY_READY)

    async def test_after_reconnect_a_late_final_is_kept_not_voiced_into_nothing(self) -> None:
        stale = d.State(conversation="saved-1", question=d.Question(asked_at=990.0), holding="wait",
                        last_owner_at=990.0)
        h = Harness(state=stale)
        h.engine.on_gateway_started()   # адаптер шлёт его на каждом connect() (решение владельца, этап 4)
        h.engine.on_final("saved-1", "Поздний ответ.", False)
        self.assertEqual(h.engine.state.answer.parts, ("Поздний ответ.",))

    async def test_after_reconnect_a_question_that_never_reached_hermes_is_reported_lost(self) -> None:
        h = Harness(state=d.State(conversation="saved-1", question=d.Question(asked_at=990.0), holding="ask",
                                  last_owner_at=990.0))
        h.engine.on_gateway_started()
        h.clock = 1000.0 + SETTINGS.lost_after
        h.engine.tick()
        self.assertEqual(await h.engine.handle(rq("алло", mid="9")), p.REPLY_LOST)

    async def test_closed_engine_saves_nothing(self) -> None:
        saved = []
        h = Harness(budget=0.05, save=saved.append)
        await h.engine.handle(rq("вопрос"))
        await h.engine.close()
        count = len(saved)
        h.engine.on_final(h.conv, "Поздний ответ", False)
        h.engine.on_gateway_started()
        self.assertEqual(len(saved), count)


class RobustnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_events_after_close_change_nothing_and_start_no_tasks(self) -> None:
        h = Harness(budget=0.05)
        await h.engine.handle(rq("вопрос"))
        await h.engine.close()
        before = h.engine.state
        h.engine.on_final(h.conv, "Поздний ответ", False)
        h.engine.on_turn_complete(h.conv, h.ask_ids[0], False)
        h.engine.on_gateway_started()
        h.engine.tick()
        self.assertEqual(h.engine.state, before)
        self.assertEqual(h.engine._tasks, set())
        self.assertEqual(await h.engine.handle(rq("ещё", mid="2")), p.REPLY_ERROR)   # отключённый движок не спрашивает шлюз
        self.assertEqual(len(h.asks), 1)

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

    async def test_a_stale_completion_does_not_open_the_gate_for_a_newer_turn(self) -> None:
        h = Harness(budget=1.0, gate_timeout=0.5)
        h.engine.on_turn_start(h.conv, "m1", True)
        h.engine.on_turn_start(h.conv, "m2", True)
        h.engine.on_turn_complete(h.conv, "m1", True)   # запоздалое завершение прошлого хода
        task = asyncio.create_task(h.engine.handle(rq("вопрос")))
        await settle()
        self.assertEqual(h.asks, [])                      # m2 ещё идёт
        h.engine.on_turn_complete(h.conv, "m2", True)
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

    async def test_a_turn_without_complete_stops_blocking_after_the_deadline(self) -> None:
        """HERMESYAND-3: шлюз не вызвал on_processing_complete и итога нет — через срок ход потерян, фраза идёт агенту."""
        h = Harness(budget=0.05, gate_timeout=0.05)
        self.assertEqual(await h.engine.handle(rq("первый", mid="1")), p.REPLY_ACCEPTED)
        h.engine.on_turn_start(h.conv, h.ask_ids[0], False)   # complete и итога не будет
        h.clock += 901   # срок потерянного хода — 15 минут (Settings.turn_lost_after = 900)
        h.engine.tick()
        task = asyncio.create_task(h.engine.handle(rq("второй", mid="2")))
        await settle()
        self.assertEqual(h.asks[-1:], [(h.conv, "второй")])
        h.engine.on_final(h.conv, "Ответ", False)
        self.assertEqual(await task, "Ответ")

    async def test_a_restarted_internal_turn_without_complete_is_closed_after_the_deadline(self) -> None:
        """HERMESYAND-24: Hermes дозапустил ход после перезапуска шлюза, а он завис — вопрос не висит вечно."""
        h = Harness(state=d.State(conversation="saved-1", question=d.Question(asked_at=990.0), last_owner_at=990.0))
        h.engine.on_gateway_started()
        h.engine.on_turn_start("saved-1", None, True)   # complete и итога не будет
        h.clock += 899
        h.engine.tick()
        self.assertIsNotNone(h.engine.state.question)
        h.clock += 2   # срок потерянного хода — 900 с от старта внутреннего хода
        h.engine.tick()
        self.assertIsNone(h.engine.state.question)

    async def test_a_turn_with_a_final_but_no_complete_does_not_block(self) -> None:
        """HERMESYAND-3: итог был, complete не пришёл — следующая фраза уходит агенту, а не падает на гейте."""
        h = Harness(budget=1.0, gate_timeout=0.05)
        first = asyncio.create_task(h.engine.handle(rq("первый", mid="1")))
        await settle()
        h.engine.on_turn_start(h.conv, h.ask_ids[0], False)
        h.engine.on_final(h.conv, "Ответ первый", False)
        self.assertEqual(await first, "Ответ первый")   # on_turn_complete не вызываем
        task = asyncio.create_task(h.engine.handle(rq("второй", mid="2")))
        await asyncio.sleep(0.2)
        self.assertEqual(h.asks[-1:], [(h.conv, "второй")])
        h.engine.on_final(h.conv, "Ответ второй", False)
        self.assertEqual(await task, "Ответ второй")

    async def test_a_lost_turn_is_dropped_in_the_adapter_too_and_wakes_a_waiting_gate(self) -> None:
        dropped = []
        h = Harness(budget=0.05, gate_timeout=5.0, drop=dropped.append)
        await h.engine.handle(rq("первый", mid="1"))
        h.engine.on_turn_start(h.conv, h.ask_ids[0], False)
        h.engine.on_final(h.conv, "Ответ первый", False)   # итог есть — вопрос закрыт, ход не закрыт
        task = asyncio.create_task(h.engine.handle(rq("скинь ответ", mid="2",
                                                      nlu={"intents": {"hya.answer.get": {}}})))
        self.assertEqual(await task, "Ответ первый")
        h.engine._budget = 5.0   # HERMESYAND-40: 0,05 с нужны первому ходу; ожидание гейта под нагрузкой их съедало
        gated = asyncio.create_task(h.engine.handle(rq("второй", mid="3")))
        await settle()
        self.assertEqual(h.asks[-1:], [(h.conv, "первый")])   # ждёт гейта (5 с)
        h.engine._drop_turn(h.conv)
        await settle()
        self.assertEqual(h.asks[-1:], [(h.conv, "второй")])
        self.assertEqual(dropped, [h.conv])
        h.engine.on_final(h.conv, "Ответ второй", False)
        self.assertEqual(await gated, "Ответ второй")

    async def test_the_deadline_drops_the_turn_in_the_adapter(self) -> None:
        dropped = []
        h = Harness(budget=0.05, drop=dropped.append)
        await h.engine.handle(rq("первый", mid="1"))
        h.engine.on_turn_start(h.conv, h.ask_ids[0], False)
        h.clock += 901
        h.engine.tick()
        self.assertEqual(dropped, [h.conv])
        self.assertIsNone(h.engine.state.question)
        self.assertTrue(h.engine.lost(h.ask_ids[0]))

    async def test_a_timed_out_question_waiting_for_the_gate_is_never_sent(self) -> None:
        h = Harness(budget=0.01, gate_timeout=5.0)
        h.engine.on_turn_start(h.conv, None, True)
        await h.engine.handle(rq("первый", mid="1"))
        self.assertEqual(h.asks, [])
        h.clock += 901
        h.engine.tick()
        await h.engine.handle(rq("второй", mid="2"))
        self.assertEqual(h.asks, [(h.conv, "второй")])

    async def test_an_expired_ask_error_after_id_eviction_cannot_close_the_new_question(self) -> None:
        h = Harness(budget=0.001)
        release = asyncio.Event()
        self.addAsyncCleanup(h.engine.close)

        async def wait_then_fail(text):
            if text == "первый":
                await release.wait()
                raise RuntimeError("fake gateway failure")

        h.on_ask = wait_then_fail
        await h.engine.handle(rq("первый", mid="1"))
        old_id = h.ask_ids[0]
        for index in range(2, 35):
            h.clock += 901
            h.engine.tick()
            await h.engine.handle(rq("следующий", mid=str(index)))
        release.set()
        await settle()
        self.assertIsNotNone(h.engine.state.question)
        self.assertFalse(h.engine.lost(old_id))
        h.engine.on_turn_start(h.conv, h.ask_ids[-1], False)
        h.engine.on_final(h.conv, "Новый ответ", False)
        self.assertEqual(h.engine.state.answer.parts, ("Новый ответ",))

    async def test_without_a_final_and_before_the_deadline_the_gate_still_fails(self) -> None:
        h = Harness(budget=0.05, gate_timeout=0.05)
        await h.engine.handle(rq("первый", mid="1"))
        h.engine.on_turn_start(h.conv, h.ask_ids[0], False)
        h.clock += 899
        h.engine.tick()
        self.assertEqual(await h.engine.handle(rq("нет", mid="2", nlu={"intents": {"YANDEX.REJECT": {}}})),
                         "Хорошо, спросите ответ позже.")
        self.assertEqual(await h.engine.handle(rq("второй", mid="3")),
                         "Начать новый диалог или продолжить прошлый?")   # вопрос ещё в работе
        self.assertFalse(h.engine.lost(h.ask_ids[0]))

    async def test_a_late_complete_of_a_dropped_turn_does_not_close_the_new_question(self) -> None:
        h = Harness(budget=0.05, gate_timeout=0.05)
        await h.engine.handle(rq("первый", mid="1"))
        h.engine.on_turn_start(h.conv, h.ask_ids[0], False)
        h.clock += 901
        h.engine.tick()
        self.assertEqual(await h.engine.handle(rq("второй", mid="2")), p.REPLY_ACCEPTED)
        h.engine.on_turn_complete(h.conv, h.ask_ids[0], False)   # поздний complete снятого хода; новый ход ещё не стартовал
        self.assertIsNotNone(h.engine.state.question)
        self.assertEqual(h.engine.state.announce, "")

    async def test_a_request_waiting_when_the_tick_drops_the_turn_hears_a_failure(self) -> None:
        h = Harness(budget=0.05)
        await h.engine.handle(rq("первый", mid="1"))
        h.engine.on_turn_start(h.conv, h.ask_ids[0], False)
        h.engine._budget = 1.0
        h.clock += 899
        waiting = asyncio.create_task(h.engine.handle(rq("да", mid="2", nlu={"intents": {"YANDEX.CONFIRM": {}}})))
        await settle()
        h.clock += 1
        h.engine.tick()
        self.assertEqual(await waiting, p.REPLY_FAILED)

    async def test_a_send_in_the_open_turn_moves_the_deadline(self) -> None:
        h = Harness(budget=0.05)
        await h.engine.handle(rq("первый", mid="1"))
        h.engine.on_turn_start(h.conv, h.ask_ids[0], False)
        h.clock += 600
        h.engine.on_send(h.conv)
        h.clock += 600
        h.engine.tick()
        self.assertIsNotNone(h.engine.state.question)
        h.clock += 300
        h.engine.tick()
        self.assertIsNone(h.engine.state.question)

    async def test_a_failing_ask_fails_the_question_and_logs_only_the_class(self) -> None:
        h = Harness(budget=1.0)
        h.ask_error = RuntimeError("секретный текст вопроса")
        self.assertEqual(await h.engine.handle(rq("вопрос")), p.REPLY_FAILED)
        self.assertIsNone(h.engine.state.question)
        self.assertTrue(any("RuntimeError" in line for line in h.lines))
        self.assertFalse(any("секретный" in line or "вопрос" in line for line in h.lines))


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    """Telegram настроен (этап 5, срез «а»): действия `Telegram` уходят в очередь доставки, исход — событие `Delivery`."""

    def setUp(self) -> None:
        self.queued: list[tuple[str, str, tuple[str, ...]]] = []
        self.asks: list[tuple[str, str, str]] = []
        ids = (f"c{i}" for i in itertools.count(1))

        async def ask(conversation, text, mid):
            self.asks.append((conversation, text, mid))

        async def recall(conversation):
            return "found", None

        self.engine = en.Engine(d.Settings(), ask, recall, lambda: next(ids), wall=lambda: 1000.0, budget=0.2,
                                log=lambda line: None,
                                telegram=lambda conv, text, fallback: self.queued.append((conv, text, fallback)))

    async def asked(self, text: str, final: str) -> str:
        task = asyncio.create_task(self.engine.handle(rq(text, mid=str(len(self.asks) + 1))))
        await settle()
        conv = self.engine.state.conversation
        self.engine.on_turn_start(conv, self.asks[-1][2], False)
        self.engine.on_final(conv, final, False)
        self.engine.on_turn_complete(conv, self.asks[-1][2], False)
        return await task

    async def test_details_after_the_telegram_marker_are_queued_with_the_label(self) -> None:
        reply = await self.asked("вопрос", "ГОЛОС: Коротко.\nТЕЛЕГРАМ: Подробно, с кодом.")
        self.assertEqual(reply, "Коротко.")
        self.assertEqual(self.queued, [("c1", "Алиса:\nПодробно, с кодом.", ())])

    async def test_a_failed_delivery_offers_to_voice_the_answer(self) -> None:
        conv = self.engine.state.conversation
        self.engine.on_delivery(conv, "failed", "Алиса:\nВесь ответ.", ("Весь ответ.",))
        self.assertEqual(await self.engine.handle(rq("алло", mid="5")), "Не смогла отправить в Телеграм. Озвучить?")

    async def test_an_uncertain_delivery_is_warned_once_before_the_next_reply(self) -> None:
        conv = self.engine.state.conversation
        self.engine.on_delivery(conv, "uncertain", "Алиса:\nПодробно.", ())
        first = await self.engine.handle(rq("", mid="5", new=True))
        second = await self.engine.handle(rq("", mid="6", new=True))
        self.assertEqual(first, "Возможно, ответ не дошёл до Телеграма. Слушаю.")
        self.assertFalse(second.startswith("Возможно"))

    async def test_an_outside_send_is_forwarded_only_with_telegram(self) -> None:
        async def unused(*args):
            raise AssertionError("шлюз не нужен")

        self.engine.forward("c9", "Алиса:\nНапоминание.")
        off = en.Engine(SETTINGS, unused, unused, lambda: "x",
                        telegram=lambda *a: self.fail("отправка без Telegram"))
        off.forward("c9", "Алиса:\nНапоминание.")
        self.assertEqual(self.queued, [("c9", "Алиса:\nНапоминание.", ())])


class ApprovalTests(unittest.IsolatedAsyncioTestCase):
    """Подтверждения (этап 5, срез «б»): флаг «ждёт» и метка хода приходят от адаптера (`approvals.Approvals`)."""

    async def test_a_live_card_is_said_while_waiting(self) -> None:
        h = Harness(budget=0.05)
        self.assertEqual(await h.engine.handle(rq("удали каталог", mid="1")), p.REPLY_ACCEPTED)
        h.engine.on_turn_start(h.conv, h.ask_ids[0], False)
        h.engine.on_approval(h.conv, True)
        self.assertEqual(await h.engine.handle(rq("да", mid="2", nlu={"intents": {"YANDEX.CONFIRM": {}}})),
                         "Жду подтверждения в Телеграме. Подождёте?")
        h.engine.on_approval(h.conv, False)
        self.assertEqual(await h.engine.handle(rq("да", mid="3", nlu={"intents": {"YANDEX.CONFIRM": {}}})),
                         "Ещё не готово. Подождёте?")

    async def test_the_turn_mark_picks_the_voiced_text(self) -> None:
        cases = {"failed": "Запрос на подтверждение не дошёл до Телеграма, команда не выполнена.",
                 "off": "Эту команду нужно подтвердить в Телеграме, а подтверждения не настроены. Команда не выполнена.",
                 "": "Команда отклонена."}
        for card, said in cases.items():
            with self.subTest(card=card):
                h = Harness(budget=1.0)
                task = asyncio.create_task(h.engine.handle(rq("удали каталог")))
                await settle()
                h.engine.on_turn_start(h.conv, h.ask_ids[0], False)
                h.engine.on_final(h.conv, "Команда отклонена.", False, card=card)
                self.assertEqual(await task, said)


if __name__ == "__main__":
    unittest.main()
