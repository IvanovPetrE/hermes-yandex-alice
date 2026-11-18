"""Совместимость: наш AliceAdapter внутри настоящего GatewayRunner (этап 6, срез «б»).

Агент — скриптованный LLM Hermes (`tests/fakes`), голос проверяется по HTTP-ответу Алисы, вызовы шлюза к адаптеру — по журналу.
Запуск — `run.sh <checkout> <python> A|B`. Группа = один процесс = один шлюз; сценарии идут в `setUpClass`.
"""
import asyncio
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


async def sc_busy(w) -> None:
    """Вторая фраза, пока первый ход идёт: до Hermes она не доходит."""
    f = _fakes()
    w.script.add("t20a", f.Hang(6.0), f.Text("Ответ на первую."))
    w.script.add("t20b", f.Text("Ответ на вторую."))
    said = w.voice.setdefault("busy", [])
    said.append(await w.say("первая [t20a] фраза"))
    await w.wait_seen("t20a")
    said.append(await w.say("вторая [t20b] фраза"))
    while said[-1] in harness.WAITING and len(said) < 25:
        said.append(await w.say("да", confirm=True))


async def sc_next(w) -> None:
    """Сразу после озвученного итога — следующий вопрос."""
    w.script.add("t21", _fakes().Text("Ответ третий."))
    w.script.add("t22", _fakes().Text("Ответ четвёртый."))
    await w.ask("next1", "третий [t21] вопрос")
    await w.ask("next2", "четвёртый [t22] вопрос")


def mute_complete(w, token: str) -> None:
    """Шлюз «теряет» `on_processing_complete` хода с токеном (HERMESYAND-3): хук до адаптера и журнала не доходит."""
    orig = w.adapter.on_processing_complete
    muted = w.state.setdefault("muted", [])

    async def wrapper(event, outcome=None, *a, **k):
        if token in (event.text or ""):
            muted.append(event.message_id)
            return None
        return await orig(event, outcome, *a, **k)
    w.adapter.on_processing_complete = wrapper


async def sc_final_no_complete(w) -> None:
    """Итог хода пришёл, complete — нет: следующая фраза доходит до Hermes, а не падает на гейте."""
    mute_complete(w, "[t40]")
    w.script.add("t40", _fakes().Text("Ответ сороковой."))
    w.script.add("t41", _fakes().Text("Ответ сорок первый."))
    await w.ask("fnc1", "сороковой [t40] вопрос")
    await w.ask("fnc2", "сорок первый [t41] вопрос")


LOST_FINAL = "Потерянный сорок второй."


def mute_final(w, text: str) -> None:
    """Шлюз «теряет» итог хода: отправка с этим текстом до адаптера и журнала не доходит, Hermes получает успех."""
    from gateway.platforms.base import SendResult

    orig = w.adapter.send
    muted = w.state.setdefault("muted_finals", [])

    async def wrapper(chat_id, content, reply_to=None, metadata=None, *a, **k):
        if text in (content or ""):
            muted.append(None if reply_to is None else str(reply_to))
            return SendResult(success=True, message_id="muted")
        return await orig(chat_id, content, reply_to, metadata, *a, **k)
    w.adapter.send = wrapper


async def sc_lost_turn(w) -> None:
    """Hermes закончил ход, но ни итог, ни complete до адаптера не дошли: через срок тик снимает ход, новая фраза
    уходит в свободную сессию и звучит. Поздний итог снятого хода (Telegram, не голос) — unit-тесты: настоящий Hermes,
    ещё занятый ходом, отвечает на фразу из очереди с `reply_to` прошлого хода (HERMESYAND-3, compat 2026-11-18)."""
    import dataclasses

    mute_complete(w, "[t42]")
    mute_final(w, LOST_FINAL)
    engine = w.adapter._engine
    # срок больше бюджета ответа (3,5 с): иначе ход снимет уже событие Expired, и тик проверялся бы вхолостую
    engine.settings = dataclasses.replace(engine.settings, turn_lost_after=5.0)
    w.script.add("t42", _fakes().Text(LOST_FINAL))
    w.script.add("t43", _fakes().Text("Ответ сорок третий."))
    loop = asyncio.get_running_loop()
    asked = loop.time()
    said = w.voice.setdefault("lost1", [])
    said.append(await w.say("сорок второй [t42] вопрос"))
    end = asked + 15.0                                      # ход в Hermes закончился: итог заглушён
    while not w.state["muted_finals"] and loop.time() < end:
        await asyncio.sleep(0.1)
    w.state["open_before_tick"] = sorted(w.adapter._open)
    await asyncio.sleep(max(0.0, asked + 6.0 - loop.time()))   # срок вышел, событий шлюза не было
    w.adapter._engine.tick()
    w.state["open_after_tick"] = sorted(w.adapter._open)
    await w.ask("lost2", "сорок третий [t43] вопрос")


LATE_FINAL = "Поздний сорок четвёртый."
OWNER_TG, CHAT = 5001, "5001"   # синтетические: владелец и его личный чат


async def sc_busy_after_lost(w) -> None:
    """Срок потерянного хода вышел (complete заглушён), но Hermes ещё занят ходом: новая фраза уходит в занятую сессию
    (HERMESYAND-8). Известное ограничение — тест закрепляет текущую привязку, а не желаемую."""
    import dataclasses

    mute_complete(w, "[t44]")
    engine = w.adapter._engine
    engine.settings = dataclasses.replace(engine.settings, turn_lost_after=5.0)   # больше бюджета ответа 3,5 с
    w.script.add("t44", _fakes().Hang(15.0), _fakes().Text(LATE_FINAL))
    w.script.add("t45", _fakes().Text("Ответ сорок пятый."))
    loop = asyncio.get_running_loop()
    asked = loop.time()
    w.voice.setdefault("busy1", []).append(await w.say("сорок четвёртый [t44] вопрос"))
    await w.wait_seen("t44")
    w.state["open_before_tick"] = sorted(w.adapter._open)
    await asyncio.sleep(max(0.0, asked + 6.0 - loop.time()))   # срок вышел, Hermes ещё висит (Hang 15 с)
    w.adapter._engine.tick()
    w.state["open_after_tick"] = sorted(w.adapter._open)
    w.state["second_at"] = len(w.journal)
    await w.ask("busy2", "сорок пятый [t45] вопрос")
    end = loop.time() + 40.0   # оба текста должны дойти до send(): позднего итога t44 и ответа на t45
    while loop.time() < end:
        sent = " ".join(op.content for op in w.journal if op.op == "send")
        if LATE_FINAL in sent and "Ответ сорок пятый." in sent:
            break
        await asyncio.sleep(0.2)


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
        turn = next(iter(self.world.turns().values()))   # сценарий text идёт первым
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


class GroupB(_Group):
    """Занятая беседа, гейт Р2 и хук после итога; `busy_input_mode: queue` (худший случай из S1)."""

    CONFIG = "display:\n  busy_input_mode: queue\n"
    SCENARIOS = [("busy", sc_busy), ("next", sc_next)]

    def test_second_phrase_does_not_reach_hermes_while_turn_is_open(self) -> None:
        self.scenario_ok("busy")
        self.assertNotIn("t20b", self.world.script.seen)
        self.assertFalse([op for op in self.world.journal if op.op == "ask" and "[t20b]" in op.content])
        self.assertEqual(self.world.voice["busy"][-1], "Ответ на первую.")

    def test_gate_r2_no_message_while_turn_is_open(self) -> None:
        self.scenario_ok("next")
        self.assertEqual(self.world.voice["next1"][-1], "Ответ третий.")
        self.assertEqual(self.world.voice["next2"][-1], "Ответ четвёртый.")
        open_turns, asks = 0, 0
        for op in self.world.journal:
            if op.op == "ask":
                asks += 1
                self.assertEqual(open_turns, 0, "шлюзу передали фразу при открытом ходе")
            elif op.op == "start":
                open_turns += 1
            elif op.op == "complete":
                open_turns -= 1
        self.assertEqual(asks, 3)

    def test_complete_hook_after_final(self) -> None:
        turns = self.world.turns()
        self.assertEqual(len(turns), 3)
        for mid, turn in turns.items():
            with self.subTest(turn=mid):
                self.assertEqual(len(turn["finals"]), 1)
                self.assertTrue(turn["start"] < turn["finals"][0] < turn["complete"])


class GroupG(_Group):
    """Гейт Р2 при пропаже `on_processing_complete` (HERMESYAND-3); `busy_input_mode: queue`."""

    CONFIG = "display:\n  busy_input_mode: queue\n"
    SCENARIOS = [("final_no_complete", sc_final_no_complete), ("lost_turn", sc_lost_turn)]

    def _mids(self, token: str) -> list:
        return [op.mid for op in self.world.journal if op.op == "ask" and token in op.content]

    def test_a_final_without_complete_does_not_block_the_next_phrase(self) -> None:
        self.scenario_ok("final_no_complete")
        self.assertEqual(self.world.voice["fnc1"][-1], "Ответ сороковой.")
        self.assertEqual(self.world.voice["fnc2"][-1], "Ответ сорок первый.")
        (mid,) = self._mids("[t40]")
        self.assertIn(mid, self.world.state["muted"], "complete хода t40 не был заглушён: проверка была бы пустой")
        self.assertIsNone(self.world.turns()[mid]["complete"])
        self.assertEqual(len(self._mids("[t41]")), 1)

    def test_a_lost_turn_is_dropped_on_tick_and_the_next_phrase_is_answered(self) -> None:
        self.scenario_ok("lost_turn")
        self.assertEqual(self.world.voice["lost1"], ["Вопрос принят. Спросите ответ позже или готовы подождать?"])
        (mid,) = self._mids("[t42]")
        self.assertIn(mid, self.world.state["muted"], "complete хода t42 не был заглушён")
        self.assertEqual(self.world.state["muted_finals"], [mid], "итог хода t42 не был заглушён")
        self.assertEqual(len(self.world.state["open_before_tick"]), 1, "до тика хода не было: проверка была бы пустой")
        self.assertEqual(self.world.state["open_after_tick"], [], "тик не снял запись хода в адаптере")
        self.assertEqual(len(self._mids("[t43]")), 1, "фраза после срока не дошла до Hermes")
        self.assertEqual(self.world.voice["lost2"][-1], "Ответ сорок третий.")
        spoken = " ".join(s for lines in self.world.voice.values() for s in lines)
        self.assertNotIn("Не получилось", spoken)


class _BusyGroup(unittest.TestCase):
    """Фраза в занятую сессию после срока потерянного хода (HERMESYAND-8): известное ограничение, не желаемая привязка.
    Telegram — `TelegramStandin`: видно, что и в каком виде ушло владельцу."""

    BUSY_MODE = ""

    @classmethod
    def setUpClass(cls) -> None:
        from tests.fakes.platforms.telegram_standin import TelegramStandin

        cls.standin = TelegramStandin()
        cls.standin.start()
        try:
            cls.world = harness.World(
                # `extra` плагина (продолжает раздел из World.prepare): отправитель Telegram берёт адрес Bot API отсюда
                f"      telegram_base_url: {cls.standin.api_base}\n"
                "  telegram:\n    enabled: true\n    extra:\n"   # свой адаптер Telegram шлюза
                f"      base_url: {cls.standin.api_base}\n      base_file_url: {cls.standin.file_base}\n"
                f"display:\n  busy_input_mode: {cls.BUSY_MODE}\n",
                env_extra=(f"TELEGRAM_BOT_TOKEN={cls.standin.token}\nTELEGRAM_ALLOWED_USERS={OWNER_TG}\n"
                           "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS=1\nHERMES_TELEGRAM_TEXT_BATCH_DELAY_SECONDS=0\n"
                           f"ALICE_TELEGRAM_CHAT={CHAT}\nALICE_TELEGRAM_OWNER_ID={OWNER_TG}\n"))
            cls.world.run([("busy_after_lost", sc_busy_after_lost)])
            cls.telegram = [v.text for v in cls.standin.visible(CHAT)]
            print(cls.standin.describe())
        finally:
            cls.standin.stop()
        w = cls.world
        print(f"\n=== {cls.BUSY_MODE}: errors={w.errors} voice={w.voice}\n    state={w.state}\n    telegram={cls.telegram}")
        for op in w.journal:
            print(f"    {op.t:9.2f} {op.op:8} mid={op.mid} reply_to={op.reply_to} meta={op.metadata} {op.content[:70]!r}")

    def _mids(self, token: str) -> list:
        return [op.mid for op in self.world.journal if op.op == "ask" and token in op.content]

    def check_common(self) -> None:
        self.assertNotIn("busy_after_lost", self.world.errors, self.world.errors.get("busy_after_lost"))
        (mid,) = self._mids("[t44]")
        self.assertIn(mid, self.world.state["muted"], "complete хода t44 не был заглушён: сцена ничего не доказывает")
        self.assertEqual(len(self.world.state["open_before_tick"]), 1, "до тика хода не было: проверка была бы пустой")
        self.assertEqual(self.world.state["open_after_tick"], [], "тик не снял запись хода в адаптере")
        self.assertEqual(len(self._mids("[t45]")), 1, "фраза после срока не дошла до Hermes")
        self.assertIn(LATE_FINAL, " ".join(op.content for op in self.world.journal if op.op == "send"),
                      "поздний итог t44 не дошёл до адаптера: сцена ничего не доказывает")


class GroupQ(_BusyGroup):
    """Фраза в занятую сессию, `busy_input_mode: queue` (худший случай из S1). Известное ограничение: Hermes обрабатывает
    фразу отдельным ходом, но `on_processing_complete` приходит раньше итога, а итог — с `reply_to` прошлого хода."""

    BUSY_MODE = "queue"

    def test_second_phrase_reaches_hermes_and_the_late_final_goes_to_telegram_not_voice(self) -> None:
        self.check_common()
        spoken = " ".join(s for lines in self.world.voice.values() for s in lines)
        self.assertNotIn(LATE_FINAL, spoken, "поздний итог прошлого хода не должен звучать")
        self.assertIn("Алиса:\n" + LATE_FINAL, self.telegram, "поздний итог не пришёл в Telegram")


class GroupI(_BusyGroup):
    """То же при `busy_input_mode: interrupt`. Утверждения уточняются по первому прогону на сервере (в S1 для этого
    режима: «↪ Redirected…» с `reply_to` второй фразы, хуки только у первой)."""

    BUSY_MODE = "interrupt"

    def test_second_phrase_reaches_hermes(self) -> None:
        self.check_common()


if __name__ == "__main__":
    unittest.main()
