"""Совместимость: «повтори» читает последний ответ из `state.db` Hermes (S7, этап 6, срез «в»).

Наш AliceAdapter в настоящем GatewayRunner; «повтори» — интент `YANDEX.REPEAT` по HTTP. Группы (run.sh, по процессу):
`H` — голос в профиле-цели маршрута (раскладка A2, симлинк), `N` — без маршрута (профиль по умолчанию), `Z` — сжатие беседы.
Перезапуски (рестарт, автопродолжение, сбой провайдера в истории) — `test_compat_restart.py`.
Отдельные группы нужны ещё и из-за лимита Д16 (6 ходов агента в минуту на шлюз).
"""
import time
import unittest

import harness

TARGET = "target"
REPEAT, NEW, REJECT = "YANDEX.REPEAT", "hya.dialog.new", "YANDEX.REJECT"
FAILED = "Не получилось, попробуйте ещё раз."
NOTHING = "Пока нечего повторить."
CONTINUE = "Продолжить?"
READ_DEADLINE = 1.5   # срок чтения истории в адаптере (`Engine.recall_timeout`); бюджет ответа Алисы — ~3,5 с


def _fakes():
    from tests.fakes import fake_llm_provider as f
    return f


async def repeat(w, name: str) -> str:
    reply = await w.say("повтори", intents=(REPEAT,))
    w.voice.setdefault(name, []).append(reply)
    return reply


async def sc_nothing(w) -> None:
    """Беседа без итога: «повтори» — «Пока нечего повторить» (сессии в истории нет)."""
    await repeat(w, "nothing")


async def sc_plain(w) -> None:
    """Обычный ход, затем «повтори»: тот же текст из истории; время чтения — в пределах срока чтения."""
    w.script.add("h01", _fakes().Text("Ответ первый."))
    await w.ask("plain", "расскажи [h01] вопрос")
    t0 = time.monotonic()
    await repeat(w, "plain_repeat")
    w.state["repeat_seconds"] = time.monotonic() - t0


async def sc_tool(w) -> None:
    """Ход с инструментом: комментарий «Сейчас проверю.» лежит в истории с tool_calls — «повтори» даёт итог."""
    f = _fakes()
    w.script.add("h02", f.ToolCall("terminal", {"command": "echo hi"}, text="Сейчас проверю."), f.Text("Готово: hi."))
    await w.ask("tool", "проверь [h02] терминал")
    await repeat(w, "tool_repeat")


async def sc_empty(w) -> None:
    """Пустой ответ модели: в истории `(empty)` — ошибка шлюза по форме, а не прошлый хороший ответ."""
    w.script.add("h03", fallback=_fakes().Text(""))
    await w.ask("empty", "пустой [h03] ответ")
    await repeat(w, "empty_repeat")


async def sc_error(w) -> None:
    """Сбой провайдера: «повтори» не должен вернуть прошлый хороший ответ («Готово: hi.» после sc_tool)."""
    w.script.add("h04", fallback=_fakes().Error(500, "scripted failure"))
    await w.ask("error", "сбой [h04] провайдера")
    await repeat(w, "error_repeat")


async def sc_new(w) -> None:
    """«Новый диалог»: прошлая беседа не читается."""
    w.voice["new"] = [await w.say("новый диалог", intents=(NEW,))]
    await repeat(w, "new_repeat")


async def ask_long(w, name: str, text: str) -> None:
    """Длинный ответ звучит по частям с «Продолжить?»; «нет» закрывает чтение (Telegram выключен)."""
    reply = await w.ask(name, text)
    while reply.endswith(CONTINUE):
        reply = await w.say("нет", intents=(REJECT,))
        w.voice[name].append(reply)


async def sc_compress(w) -> None:
    """Ходы с большими токенами, пока Hermes не сожмёт беседу на месте; затем ещё один ход и «повтори»."""
    w.big = True
    try:
        for i in range(1, 6):   # пять ходов + «zafter» = 6 ходов агента: предел Д16 в минуту
            token = f"z{i}"
            w.script.add(token, _fakes().Text(f"Ответ {token}. " + "Подробности беседы. " * 300))
            await ask_long(w, token, f"вопрос [{token}]")
            view = w.target_db_view()
            w.state["compress_view"] = view
            if any(s["rows"] > s["message_count"] for s in view.get("sessions", [])):
                w.state["compressed_after"] = i
                break
    finally:
        w.big = False
    w.script.add("zafter", _fakes().Text("Ответ после сжатия."))
    await w.ask("zafter", "ещё [zafter] вопрос")
    view = w.target_db_view()
    w.state["final_view"] = view
    if "compressed_after" not in w.state and any(s["rows"] > s["message_count"] for s in view.get("sessions", [])):
        w.state["compressed_after"] = "zafter"
    await repeat(w, "zafter_repeat")


class _Group(unittest.TestCase):
    ROUTE: "str | None" = None
    TARGET_EXTRA = ""
    SCENARIOS: list = []

    @classmethod
    def setUpClass(cls) -> None:
        cls.world = harness.World(route_profile=cls.ROUTE, target_extra=cls.TARGET_EXTRA)
        cls.world.run(cls.SCENARIOS)
        print(f"\n=== errors={cls.world.errors} voice={cls.world.voice} state={cls.world.state}")

    def scenario_ok(self, name: str) -> None:
        self.assertNotIn(name, self.world.errors, self.world.errors.get(name))


class GroupH(_Group):
    """Голос в профиле-цели маршрута (A2 с симлинком)."""

    ROUTE = TARGET
    SCENARIOS = [("nothing", sc_nothing), ("plain", sc_plain), ("tool", sc_tool), ("error", sc_error),
                 ("empty", sc_empty), ("new", sc_new)]

    def test_nothing_to_repeat_without_a_session(self) -> None:
        self.scenario_ok("nothing")
        self.assertEqual(self.world.voice["nothing"], [NOTHING])

    def test_plain_answer_is_repeated_from_target_history(self) -> None:
        self.scenario_ok("plain")
        self.assertEqual(self.world.voice["plain"][-1], "Ответ первый.")
        self.assertEqual(self.world.voice["plain_repeat"], ["Ответ первый."])
        self.assertTrue(self.world.target_db_view().get("sessions"), "голосовая сессия должна лежать в state.db цели")

    def test_read_time_is_within_the_read_deadline(self) -> None:
        self.scenario_ok("plain")
        self.assertLess(self.world.state["repeat_seconds"], READ_DEADLINE)   # чтение под срок, не от p50 пробы

    def test_tool_turn_repeats_the_final_not_the_comment(self) -> None:
        self.scenario_ok("tool")
        self.assertEqual(self.world.voice["tool"][-1], "Готово: hi.")
        self.assertEqual(self.world.voice["tool_repeat"], ["Готово: hi."])

    def test_provider_error_never_repeats_the_old_answer(self) -> None:
        self.scenario_ok("error")
        self.assertEqual(self.world.voice["error"][-1], FAILED)
        self.assertNotEqual(self.world.voice["error_repeat"], ["Готово: hi."])   # прошлый хороший ответ не возвращается

    def test_known_limitation_provider_error_text_is_repeated_verbatim(self) -> None:
        """Известное ограничение (docs/PLAN.md, «Открыто»): при сбое провайдера Hermes пишет в историю английский текст
        «Your request was not processed…»; `protocol.is_gateway_error` его не узнаёт, и «повтори» зачитывает его
        владельцу вместо «Не получилось». Ожидаемо после починки — `[FAILED]`."""
        self.scenario_ok("error")
        self.assertEqual(self.world.voice["error_repeat"],
                         ["Your request was not processed. Send it again if you still want me to carry it out."])

    def test_empty_answer_repeats_as_failure(self) -> None:
        self.scenario_ok("empty")
        self.assertEqual(self.world.voice["empty"][-1], FAILED)
        self.assertEqual(self.world.voice["empty_repeat"], [FAILED])

    def test_new_dialog_does_not_read_the_old_one(self) -> None:
        self.scenario_ok("new")
        self.assertEqual(self.world.voice["new_repeat"], [NOTHING])


class GroupN(_Group):
    """Голос в профиле по умолчанию, маршрута нет (C1 пробы)."""

    SCENARIOS = [("nothing", sc_nothing), ("plain", sc_plain), ("new", sc_new)]

    def test_default_profile_conversation_is_read(self) -> None:
        self.scenario_ok("plain")
        self.assertEqual(self.world.voice["plain_repeat"], ["Ответ первый."])

    def test_unknown_and_new_conversation_have_nothing(self) -> None:
        self.scenario_ok("nothing")
        self.scenario_ok("new")
        self.assertEqual(self.world.voice["nothing"], [NOTHING])
        self.assertEqual(self.world.voice["new_repeat"], [NOTHING])


class GroupZ(_Group):
    """Сжатие беседы на месте: «повтори» после него читает свежий ответ."""

    ROUTE = TARGET
    TARGET_EXTRA = "compression:\n  protect_last_n: 2\n"   # как в пробе S7: иначе защищённый хвост не даёт сжать малую беседу
    SCENARIOS = [("compress", sc_compress)]

    def test_repeat_after_compression_reads_the_fresh_answer(self) -> None:
        self.scenario_ok("compress")
        self.assertIn("compressed_after", self.world.state,
                      f"Hermes не сжал беседу: проверка была бы пустой ({self.world.state.get('compress_view')})")
        self.assertEqual(self.world.voice["zafter_repeat"], ["Ответ после сжатия."])


if __name__ == "__main__":
    unittest.main()
