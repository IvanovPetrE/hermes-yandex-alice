"""Совместимость: наш AliceAdapter внутри настоящего GatewayRunner (этап 6, срез «б»).

Агент — скриптованный LLM Hermes (`tests/fakes`), голос проверяется по HTTP-ответу Алисы, вызовы шлюза к адаптеру — по журналу.
Запуск — `run.sh <checkout> <python> A|B`. Группа = один процесс = один шлюз; сценарии идут в `setUpClass`.
"""
import unittest

import harness


def _fakes():
    from tests.fakes import fake_llm_provider as f
    return f


async def sc_text(w) -> None:
    w.script.add("t01", _fakes().Text("Ответ первый."))
    await w.ask("text", "расскажи [t01] вопрос")


FAILED = "Не получилось, попробуйте ещё раз."


async def sc_tool(w) -> None:
    f = _fakes()
    w.script.add("t02", f.ToolCall("terminal", {"command": "echo hi"}, text="Сейчас проверю."), f.Text("Готово: hi."))
    await w.ask("tool", "проверь [t02] терминал")


async def sc_empty(w) -> None:
    w.script.add("t03", fallback=_fakes().Text(""))   # модель отвечает пусто каждый раз
    await w.ask("empty", "пустой [t03] ответ")


async def sc_error(w) -> None:
    w.script.add("t04", fallback=_fakes().Error(500, "scripted failure"))   # провайдер падает каждый раз
    await w.ask("error", "сбой [t04] провайдера")


class _Group(unittest.TestCase):
    CONFIG = ""
    SCENARIOS: list = []

    @classmethod
    def setUpClass(cls) -> None:
        cls.world = harness.World(cls.CONFIG)
        cls.world.run(cls.SCENARIOS)
        print(f"\n=== errors={cls.world.errors} voice={cls.world.voice}")

    def scenario_ok(self, name: str) -> None:
        self.assertNotIn(name, self.world.errors, self.world.errors.get(name))


class GroupA(_Group):
    """Итоги хода; `busy_input_mode: interrupt`."""

    CONFIG = "display:\n  busy_input_mode: interrupt\n"
    SCENARIOS = [("text", sc_text), ("tool", sc_tool), ("empty", sc_empty), ("error", sc_error)]

    def test_gateway_started_our_adapter(self) -> None:
        self.assertTrue(self.world.state["connected"])
        self.assertEqual(self.world.state["adapter_module"].rpartition(".")[2], "adapter")

    def test_text(self) -> None:
        self.scenario_ok("text")
        self.assertEqual(self.world.voice["text"][-1], "Ответ первый.")
        (turn,) = self.world.turns().values()
        self.assertEqual(len(turn["finals"]), 1, "ровно один итог с reply_to хода и notify=True")
        self.assertLess(turn["finals"][0], turn["complete"], "итог раньше on_processing_complete")

    def test_tool(self) -> None:
        self.scenario_ok("tool")
        self.assertEqual(self.world.voice["tool"][-1], "Готово: hi.")   # «Сейчас проверю.» не итог

    def test_empty_model_answer_is_failure(self) -> None:
        self.scenario_ok("empty")
        self.assertEqual(self.world.voice["empty"][-1], FAILED)

    def test_provider_error_is_failure(self) -> None:
        self.scenario_ok("error")
        self.assertEqual(self.world.voice["error"][-1], FAILED)

    def test_every_turn_has_start_final_complete_in_order(self) -> None:
        turns = self.world.turns()
        self.assertEqual(len(turns), 4)
        for mid, turn in turns.items():
            with self.subTest(turn=mid):
                self.assertEqual(len(turn["finals"]), 1)
                self.assertTrue(turn["start"] < turn["finals"][0] < turn["complete"])

    def test_home_channel_notice_is_not_voiced(self) -> None:
        notices = [op for op in self.world.journal if op.op == "send" and op.reply_to is None and "📬" in op.content]
        self.assertTrue(notices, "в журнале нет уведомления «📬 No home channel»: проверка была бы пустой")
        spoken = " ".join(s for lines in self.world.voice.values() for s in lines)
        self.assertNotIn("📬", spoken)
        self.assertNotIn("home channel", spoken.lower())


if __name__ == "__main__":
    unittest.main()
