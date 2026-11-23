"""Совместимость: жизненный цикл адаптера и перезапуск шлюза посреди хода (S5, этап 6, срез «в»).

Шлюз — дочерний процесс (`gateway_child.py`), его можно остановить штатно или убить; каждая сцена — свой дом (счётчик
перезапусков Hermes считается по дому). Агент — скриптованный LLM Hermes, голос проверяется по HTTP-ответам Алисы, вызовы
шлюза к адаптеру — по журналу. Группы (run.sh, по процессу): `L` — один listener, переподключение, reload, остановка, занятый
порт; `R` — перезапуск посреди хода (штатный и SIGKILL), леджер «♻️», новая фраза во время дозапущенного хода.
Настройки Hermes по умолчанию (`interactive_resume=True`) для адаптера недостижимы: атрибут класса задан в `adapter.py`.
"""
import asyncio
import threading
import unittest

import harness
from restart import Foreign, RestartWorld, listeners, port_is_free

REPEAT = "YANDEX.REPEAT"
FAILED = "Не получилось, попробуйте ещё раз."
RESUMED = "Ответ после перезапуска."
NOT_WAITING = lambda reply: reply not in harness.WAITING   # noqa: E731


def _fakes():
    from tests.fakes import fake_llm_provider as f
    return f


async def call(child, op, timeout=60.0, **kw):
    return await asyncio.to_thread(child.cmd, op, timeout, **kw)


async def poll(pred, timeout: float, step: float = 0.5):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        got = pred()
        if got:
            return got
        await asyncio.sleep(step)
    return None


# --- группа L: жизненный цикл ----------------------------------------------------------------------------------------

async def sc_life(w) -> None:
    f = _fakes()
    for token in ("l01", "l02", "l04"):
        w.script.add(token, f.Text(f"Ответ {token}."))
    child = await w.start()
    st = w.state
    st["listeners_start"] = listeners(child.pid, w.port)
    await w.ask("l01", "первый [l01] вопрос")
    info0 = await call(child, "info")
    reloads = [await call(child, "reload"), await call(child, "reload")]
    info1 = await call(child, "info")
    st["reload"] = dict(ok=[bool(r and r.get("ok")) for r in reloads], same_adapter=info0["alice_id"] == info1["alice_id"],
                        listeners=listeners(child.pid, w.port),
                        adapters=sum(1 for op in w.journal if op.op == "adapter" and op.pid == child.pid))
    await w.ask("l02", "второй [l02] вопрос после reload")
    # R6 пробы S5: исключение в обработчике шлюза — владелец слышит «не получилось», текст ошибки Hermes не озвучивается
    await call(child, "arm_boom")
    await w.ask("boom", "сбой [boom] обработчика")
    # L4: переподключение шлюзом после повторяемой ошибки платформы
    await call(child, "fatal", 30.0)

    async def reconnected():
        info = await call(child, "info", 10.0)
        if info and info["alice_id"] not in (None, info1["alice_id"]) and listeners(child.pid, w.port) == 1:
            return info
        return None

    st["reconnected"] = None
    for _ in range(240):
        st["reconnected"] = await reconnected()
        if st["reconnected"]:
            break
        await asyncio.sleep(1)
    st["adapters_after_reconnect"] = sum(1 for op in w.journal if op.op == "adapter" and op.pid == child.pid)
    st["listeners_reconnect"] = listeners(child.pid, w.port)
    await w.ask("l04", "четвёртый [l04] вопрос после переподключения")
    st["wrong_path"] = await w.raw_status("wrong-" + w.secret[::-1], b"{}")
    st["bad_body"] = await w.raw_status(w.secret, b"not json")
    st["stopped_clean"] = await w.stop(child)
    st["port_free"] = port_is_free(w.port)
    st["secret_in_logs"] = w.secret_in_logs(child.lines)


async def sc_reconnect_mid_turn(w) -> None:
    """Итог хода, открытого прежним экземпляром адаптера, после переподключения приходит новому экземпляру: ход общий
    на регистрацию, итог остаётся итогом и звучит голосом."""
    f = _fakes()
    hold = w.script.hold("l05")
    w.script.add("l05", f.Text("Ответ l05."))
    child = await w.start()
    first = await w.say("долгий [l05] вопрос")
    await w.wait_seen("l05")
    before = (await call(child, "info"))["alice_id"]
    await call(child, "fatal", 30.0)
    w.state["new_adapter"] = None
    for _ in range(240):
        info = await call(child, "info", 10.0)
        if info and info["alice_id"] not in (None, before):
            w.state["new_adapter"] = info
            break
        await asyncio.sleep(1)
    hold.set()
    w.state["final_sent"] = await w.wait_op(lambda op: op.op == "send" and "Ответ l05." in op.content, 30.0)
    w.voice["mid_turn_reconnect"] = [first]
    await w.confirm_until("mid_turn_reconnect", NOT_WAITING)
    await w.stop(child)


async def sc_busy(w) -> None:
    """Порт занят чужим listener'ом: шлюз жив, адаптер повторяет попытки и подключается сам после освобождения."""
    foreign = w.state["foreign"]
    f = _fakes()
    w.script.add("b01", f.Text("Ответ b01."))
    child = await w.start()
    st = w.state
    st["alice_listeners"] = listeners(child.pid, w.port)
    st["gateway_alive"] = child.alive()
    before = foreign.accepted
    st["foreign_poke"] = foreign.poke()
    await asyncio.sleep(0.5)
    st["foreign_accepts"] = foreign.accepted - before
    info = await call(child, "info")
    st["queued"] = (info.get("failed") or {}).get(harness.PLATFORM)
    foreign.close()
    st["recovered"] = await poll(lambda: listeners(child.pid, w.port) == 1, 300.0, 1.0)
    await w.ask("b01", "вопрос [b01] после освобождения порта")
    st["wrong_path"] = await w.raw_status("wrong-" + w.secret[::-1], b"{}")
    st["bad_body"] = await w.raw_status(w.secret, b"not json")
    st["info_after"] = await call(child, "info")
    st["stopped_clean"] = await w.stop(child)
    st["secret_in_logs"] = w.secret_in_logs(child.lines)


class _Group(unittest.TestCase):
    @classmethod
    def run_scene(cls, name: str, fn, **kw) -> RestartWorld:
        w = RestartWorld(name, **kw)
        try:
            w.run([(name, fn)])
        finally:
            print(f"\n=== {name}: errors={w.errors} voice={w.voice} state="
                  f"{ {k: v for k, v in w.state.items() if k != 'foreign'} }")
        return w

    @staticmethod
    def scenario_ok(w, name: str) -> None:
        if name in w.errors:
            raise AssertionError(w.errors[name])


class GroupL(_Group):
    """Жизненный цикл адаптера и занятый порт (L1–L5, R6, P1, P2 пробы S5)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.life = cls.run_scene("life", sc_life, config_extra="display:\n  busy_input_mode: queue\n")
        cls.reconn = cls.run_scene("reconn", sc_reconnect_mid_turn, platform_lines="")
        foreign = Foreign()
        try:
            cls.busy = RestartWorld("busy", port=foreign.port, config_extra="logging:\n  level: DEBUG\n")
            cls.busy.state["foreign"] = foreign
            cls.busy.run([("busy", sc_busy)])
        finally:
            foreign.close()
            print(f"\n=== busy: errors={cls.busy.errors} voice={cls.busy.voice} state="
                  f"{ {k: v for k, v in cls.busy.state.items() if k != 'foreign'} }")

    def test_one_listener_and_turns_work(self) -> None:
        self.scenario_ok(self.life, "life")
        st = self.life.state
        self.assertEqual(st["listeners_start"], 1)
        self.assertEqual(self.life.voice["l01"][-1], "Ответ l01.")

    def test_reload_keeps_the_adapter_and_the_listener(self) -> None:
        self.scenario_ok(self.life, "life")
        r = self.life.state["reload"]
        self.assertEqual(r["ok"], [True, True])
        self.assertTrue(r["same_adapter"])
        self.assertEqual((r["listeners"], r["adapters"]), (1, 1), "reload не должен создавать новый адаптер")
        self.assertEqual(self.life.voice["l02"][-1], "Ответ l02.")

    def test_handler_exception_is_failure_not_error_text(self) -> None:
        self.scenario_ok(self.life, "life")
        self.assertEqual(self.life.voice["boom"][-1], FAILED)
        spoken = " ".join(s for lines in self.life.voice.values() for s in lines)
        self.assertNotIn("Sorry", spoken)

    def test_gateway_reconnect_gives_a_new_adapter_and_one_listener(self) -> None:
        self.scenario_ok(self.life, "life")
        st = self.life.state
        self.assertIsNotNone(st["reconnected"], "адаптер не переподключился")
        self.assertEqual(st["adapters_after_reconnect"], 2)
        self.assertEqual(st["listeners_reconnect"], 1)
        self.assertEqual(self.life.voice["l04"][-1], "Ответ l04.")

    def test_stop_frees_the_port_and_negative_http(self) -> None:
        self.scenario_ok(self.life, "life")
        st = self.life.state
        self.assertTrue(st["stopped_clean"])
        self.assertTrue(st["port_free"])
        self.assertEqual((st["wrong_path"], st["bad_body"]), (404, 400))

    def test_no_secret_in_logs(self) -> None:
        self.scenario_ok(self.life, "life")
        found = self.life.state["secret_in_logs"]
        self.assertIn("logs/agent.log", found, "нет agent.log: проверка была бы пустой")
        self.assertFalse([k for k, v in found.items() if v], found)

    def test_final_of_a_turn_opened_before_reconnect_is_voiced(self) -> None:
        self.scenario_ok(self.reconn, "reconn")
        self.assertIsNotNone(self.reconn.state["new_adapter"], "адаптер не переподключился")
        self.assertIsNotNone(self.reconn.state["final_sent"], "итог не дошёл до адаптера: сцена ничего не доказывает")
        self.assertEqual(self.reconn.voice["mid_turn_reconnect"][-1], "Ответ l05.")

    def test_busy_port_gateway_alive_and_foreign_socket_untouched(self) -> None:
        self.scenario_ok(self.busy, "busy")
        st = self.busy.state
        self.assertEqual(st["alice_listeners"], 0)
        self.assertTrue(st["gateway_alive"])
        self.assertTrue(st["foreign_poke"])
        self.assertEqual(st["foreign_accepts"], 1)
        self.assertIsNotNone(st["queued"], "платформа должна ждать в очереди переподключения")
        self.assertFalse(st["queued"]["paused"])

    def test_busy_port_adapter_connects_by_itself_once_free(self) -> None:
        self.scenario_ok(self.busy, "busy")
        st = self.busy.state
        self.assertTrue(st["recovered"], "адаптер не подключился за 300 с после освобождения порта")
        self.assertEqual(self.busy.voice["b01"][-1], "Ответ b01.")
        self.assertEqual((st["wrong_path"], st["bad_body"]), (404, 400))
        self.assertFalse((st["info_after"] or {}).get("failed"))

    def test_busy_port_no_secret_in_debug_logs(self) -> None:
        self.scenario_ok(self.busy, "busy")
        found = self.busy.state["secret_in_logs"]
        self.assertIn("logs/agent.log", found, "нет agent.log: проверка была бы пустой")
        self.assertFalse([k for k, v in found.items() if v], found)


# --- группа R: перезапуск посреди хода --------------------------------------------------------------------------------

R_PLATFORM = "    gateway_restart_notification: false\n"   # как в боевой настройке (docs): уведомление о перезапуске не нужно
R_CONFIG = "display:\n  busy_input_mode: queue\n"


async def mid_turn_restart(w, how: str, token: str, *, hold_resume: bool = False):
    """Ход держится в модели, шлюз останавливают (`stop`) или убивают (`kill`) и запускают заново в том же доме."""
    f = _fakes()
    hold = w.script.hold(token)
    w.script.add(token, f.Text("Ответ первого запуска."))
    if hold_resume:
        w.script.resume_hold = threading.Event()
    first = await w.start()
    w.voice[token] = [await w.say(f"долгий [{token}] вопрос")]
    await w.wait_seen(token)
    if how == "stop":
        w.state["stopped_clean"] = await w.stop(first)
    else:
        await w.kill(first)
    hold.set()   # поток мёртвого запуска не должен висеть
    second = await w.start()
    return first, second


def internal_start(w, child):
    return next((op for op in w.journal if op.op == "start" and op.pid == child.pid
                 and op.metadata.get("internal")), None)


async def sc_resume(w, how: str, token: str) -> None:
    first, second = await mid_turn_restart(w, how, token)
    reply = await w.confirm_until(token, NOT_WAITING)
    w.state["reply"] = reply
    w.state["internal"] = internal_start(w, second)
    w.state["notes"] = list(w.script.resume_notes)
    w.state["repeat"] = await w.say("повтори", intents=(REPEAT,))


async def sc_resume_stop(w) -> None:
    """Штатная остановка посреди хода. Исход у Hermes v0.23.2 недетерминирован (HERMESYAND-35): агент разматывается за
    ~30 мс, но итог прерванного хода и отмена задачи при разрыве адаптера гоняются. Либо ход дозапускается (внутренний
    ход, ответ голосом), либо итог успел лечь в транскрипт и `_ledger_crash_left_replies` ставит его в долг доставки
    вместо дозапуска — владелец слышит «Не получилось»; исход `first_complete` — `success` или `cancelled` в обоих случаях."""
    first, second = await mid_turn_restart(w, "stop", "r01")
    w.state["first_complete"] = next((op for op in w.journal if op.op == "complete" and op.pid == first.pid), None)
    w.state["first_finals"] = [op for op in w.journal if op.op == "send" and op.pid == first.pid and op.reply_to]
    w.state["reply"] = await w.confirm_until("r01", NOT_WAITING)
    resumed = None
    if w.state["reply"] == RESUMED:   # ветка «долг доставки» дозапуска не ждёт: 60 с впустую
        resumed = await w.wait_op(lambda op: op.op == "send" and op.pid == second.pid and RESUMED in op.content, 60.0)
    w.state["internal"] = internal_start(w, second)
    w.state["resumed_send"] = resumed
    w.state["notes"] = list(w.script.resume_notes)
    w.state["repeat"] = await w.say("повтори", intents=(REPEAT,))


async def sc_resume_kill(w) -> None:
    await sc_resume(w, "kill", "r02")


async def sc_idle_restart(w) -> None:
    """Штатный перезапуск в простое (B1 пробы S7): беседа та же, «повтори» читает прежний ответ, следующий ход
    дописывается в ту же сессию."""
    f = _fakes()
    w.script.add("r06", f.Text("Ответ r06."))
    w.script.add("r07", f.Text("Ответ r07."))
    first = await w.start()
    await w.ask("r06", "первый [r06] вопрос")
    w.state["stopped_clean"] = await w.stop(first)
    w.state["sessions_before"] = len(w.target_db_view().get("sessions", []))
    await w.start()
    w.state["repeat_after_restart"] = await w.say("повтори", intents=(REPEAT,))
    await w.ask("r07", "второй [r07] вопрос")
    w.state["repeat_next"] = await w.say("повтори", intents=(REPEAT,))
    w.state["sessions_after"] = len(w.target_db_view().get("sessions", []))


async def sc_ledger(w) -> None:
    """SIGKILL внутри итоговой send(): леджер Hermes досылает текст с пометкой «♻️…:» вне хода."""
    f = _fakes()
    w.script.add("r03", f.Text("Ответ r03."))
    (w.home / "hold_final").write_text("1", encoding="utf-8")
    first = await w.start()
    w.voice["r03"] = [await w.say("вопрос [r03] итог зависнет")]
    held = await w.wait_op(lambda op: op.op == "held", 60.0)
    w.state["held"] = held is not None
    await w.kill(first)
    (w.home / "hold_final").unlink()
    mark = len(w.journal)
    second = await w.start()
    w.state["redelivered"] = await w.wait_op(
        lambda op: op.op == "send" and op.pid == second.pid and "r03" in op.content, 60.0, mark)
    reply = await w.confirm_until("r03", NOT_WAITING)
    w.state["reply"] = reply
    w.state["sends"] = [op for op in w.journal if op.op == "send" and op.pid == second.pid and "r03" in op.content]
    w.state["llm_seen"] = list(w.script.seen)
    w.state["repeat"] = await w.say("повтори", intents=(REPEAT,))


OWNER_TG, CHAT = 5001, "5001"   # синтетические: владелец и его личный чат (как в группе T)


def tg_kwargs(standin) -> dict:
    """Настройки сцены с Telegram: поддельный Bot API `TelegramStandin` вместо настоящего (как в `test_compat_approval`)."""
    return dict(
        platform_lines=R_PLATFORM,
        # `extra` плагина (продолжает раздел из World.prepare): отправитель Telegram берёт адрес Bot API отсюда
        config_extra=(f"      telegram_base_url: {standin.api_base}\n"
                      "  telegram:\n    enabled: true\n    extra:\n"   # свой адаптер Telegram шлюза
                      f"      base_url: {standin.api_base}\n      base_file_url: {standin.file_base}\n" + R_CONFIG),
        env_extra=(f"TELEGRAM_BOT_TOKEN={standin.token}\nTELEGRAM_ALLOWED_USERS={OWNER_TG}\n"
                   "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS=1\nHERMES_TELEGRAM_TEXT_BATCH_DELAY_SECONDS=0\n"
                   f"ALICE_TELEGRAM_CHAT={CHAT}\nALICE_TELEGRAM_OWNER_ID={OWNER_TG}\n"))


async def sc_pending(w) -> None:
    """SIGKILL между записями леджера `pending` и `attempting` (HERMESYAND-8): Hermes после старта досылает строку без
    пометки «♻️», отправкой вне хода и без автопродолжения. Адаптер не отличает её от прочих отправок вне хода (S6):
    текст уходит в Telegram с подписью, голосом не звучит, вопрос остаётся в работе."""
    f = _fakes()
    standin = w.state["standin"]
    w.script.add("r08", f.Text("Ответ r08."))
    (w.home / "hold_pending").write_text("1", encoding="utf-8")
    first = await w.start()
    w.voice["r08"] = [await w.say("вопрос [r08] запись зависнет")]
    held = await w.wait_op(lambda op: op.op == "held_pending", 60.0)
    w.state["held"] = held is not None
    await w.kill(first)
    (w.home / "hold_pending").unlink()
    mark = len(w.journal)
    second = await w.start()
    w.state["redelivered"] = await w.wait_op(
        lambda op: op.op == "send" and op.pid == second.pid and "r08" in op.content, 60.0, mark)
    w.state["telegram"] = await poll(
        lambda: [v.text.replace("\\", "") for v in standin.visible(CHAT) if "Ответ r08" in v.text], 30.0)   # без экранов MarkdownV2
    for _ in range(3):   # голосом ответа нет: вопрос в работе, «да» только продлевает ожидание
        w.voice["r08"].append(await w.say("да", confirm=True))
    w.state["sends"] = [op for op in w.journal if op.op == "send" and op.pid == second.pid and "r08" in op.content]
    w.state["llm_seen"] = list(w.script.seen)


async def sc_new_phrase(w) -> None:
    """Новая фраза владельца, пока дозапущенный ход идёт: до Hermes не доходит, ответ один."""
    first, second = await mid_turn_restart(w, "kill", "r05", hold_resume=True)
    started = await w.wait_op(lambda op: op.op == "start" and op.pid == second.pid and op.metadata.get("internal"), 60.0)
    w.state["internal_started"] = started is not None
    w.state["intruder"] = await w.say("другая [r05b] фраза")
    w.script.resume_hold.set()
    w.state["reply"] = await w.confirm_until("r05", NOT_WAITING)
    await asyncio.sleep(2)
    w.state["finals"] = [op for op in w.journal if op.op == "send" and op.pid == second.pid and RESUMED in op.content]
    w.state["asks"] = [op for op in w.journal if op.op == "ask" and "r05b" in op.content]


class GroupR(_Group):
    """Перезапуск шлюза посреди хода (R1–R3, R5 пробы S5) и чтение итога автопродолжения из истории (S7)."""

    @classmethod
    def setUpClass(cls) -> None:
        kw = dict(platform_lines=R_PLATFORM, config_extra=R_CONFIG)
        cls.stop_w = cls.run_scene("r_stop", sc_resume_stop, **kw)
        cls.kill_w = cls.run_scene("r_kill", sc_resume_kill, **kw)
        cls.ledger_w = cls.run_scene("r_ledger", sc_ledger, **kw)
        cls.phrase_w = cls.run_scene("r_phrase", sc_new_phrase, **kw)
        cls.idle_w = cls.run_scene("r_idle", sc_idle_restart, **kw)
        from tests.fakes.platforms.telegram_standin import TelegramStandin

        standin = TelegramStandin()
        standin.start()
        try:
            cls.pending_w = RestartWorld("r_pending", **tg_kwargs(standin))
            cls.pending_w.state["standin"] = standin
            cls.pending_w.run([("r_pending", sc_pending)])
        finally:
            print(f"\n=== r_pending: errors={cls.pending_w.errors} voice={cls.pending_w.voice} state="
                  f"{ {k: v for k, v in cls.pending_w.state.items() if k != 'standin'} }")
            print(standin.describe())
            standin.stop()

    def check_resume(self, w, name: str, token: str) -> None:
        self.scenario_ok(w, name)
        st = w.state
        self.assertEqual(st["reply"], RESUMED)
        self.assertIsNotNone(st["internal"], "после перезапуска не было внутреннего хода")
        self.assertIsNone(st["internal"].mid, "у внутреннего хода message_id должен быть None")
        self.assertTrue(st["notes"], "агент не получил заметку автопродолжения")
        self.assertIn("CONTINUE the interrupted task", st["notes"][0])   # interactive_resume=False
        self.assertNotIn("Report to the user", st["notes"][0])
        spoken = " ".join(w.voice[token])
        self.assertNotIn("restored", spoken.lower())
        self.assertEqual(st["repeat"], RESUMED, "итог автопродолжения должен лежать в истории и читаться")

    def test_graceful_stop_mid_turn_resumes_and_answers(self) -> None:
        """Штатная остановка посреди хода: первый запуск получает `on_processing_complete` без итога (исход `success` или
        `cancelled`) при `GatewayRunner._draining`; адаптер вопрос не закрывает. Дальше Hermes v0.23.2 недетерминирован
        (HERMESYAND-35, гонка внутри Hermes): либо доводит ход (внутренний ход, заметка «CONTINUE») — ответ звучит
        голосом, как после SIGKILL; либо итог прерванного хода уже в транскрипте и уходит в долг доставки, дозапуска нет —
        владелец слышит «Не получилось», а не зависание в «ещё думаю»."""
        w = self.stop_w
        self.scenario_ok(w, "r_stop")
        st = w.state
        self.assertTrue(st["stopped_clean"])
        self.assertIsNotNone(st["first_complete"], "хука завершения у первого запуска не было")
        self.assertFalse(st["first_finals"], "у первого запуска не должно быть итога: сцена ничего не доказывает")
        if st["reply"] != RESUMED:   # ветка «долг доставки»: ход не дозапущен, вопрос закрыт отказом
            self.assertEqual(st["reply"], FAILED)
            self.assertIsNone(st["internal"], "ход не дозапускается, а внутренний ход есть")
            self.assertIsNone(st["resumed_send"])
            return
        self.assertIsNotNone(st["internal"], "после перезапуска не было внутреннего хода")
        self.assertIsNone(st["internal"].mid)
        self.assertIn("CONTINUE the interrupted task", st["notes"][0])   # interactive_resume=False
        self.assertNotIn("Report to the user", st["notes"][0])
        self.assertIsNotNone(st["resumed_send"], "Hermes не довёл ход после остановки")
        self.assertEqual(st["repeat"], RESUMED)

    def test_sigkill_mid_turn_resumes_and_answers(self) -> None:
        self.check_resume(self.kill_w, "r_kill", "r02")

    def test_ledger_redelivery_marker_is_cut_and_answer_is_once(self) -> None:
        w = self.ledger_w
        self.scenario_ok(w, "r_ledger")
        st = w.state
        self.assertTrue(st["held"], "итоговая send() не зависла: сцена ничего не доказывает")
        self.assertIsNotNone(st["redelivered"], "леджер не досла́л текст")
        self.assertIn("♻", st["redelivered"].content)   # Hermes присылает пометку, адаптер её срезает
        self.assertIsNone(st["redelivered"].reply_to)   # вне хода
        self.assertEqual(st["reply"], "Ответ r03.")
        self.assertNotIn("♻", " ".join(w.voice["r03"]))
        self.assertEqual(len(st["sends"]), 1, "ответ досла́н ровно один раз")
        self.assertNotIn("resume", st["llm_seen"], "автопродолжения при досылке из леджера быть не должно")
        self.assertEqual(st["repeat"], "Ответ r03.", "итог записан в историю до отправки")

    def test_ledger_pending_row_is_redelivered_plainly_to_telegram_not_voiced(self) -> None:
        w = self.pending_w
        self.scenario_ok(w, "r_pending")
        st = w.state
        self.assertTrue(st["held"], "запись леджера не зависла: сцена ничего не доказывает")
        self.assertIsNotNone(st["redelivered"], "леджер не досла́л строку pending")
        self.assertNotIn("♻", st["redelivered"].content, "строка pending должна идти без пометки")
        self.assertIsNone(st["redelivered"].reply_to)   # вне хода
        self.assertEqual(len(st["sends"]), 1, "ответ досла́н ровно один раз")
        self.assertNotIn("resume", st["llm_seen"], "автопродолжения при досылке из леджера быть не должно")
        self.assertEqual(st["telegram"], ["Алиса:\nОтвет r08."], "досылка должна уйти в Telegram с подписью")
        spoken = " ".join(w.voice["r08"])
        self.assertNotIn("Ответ r08.", spoken, "досылка без пометки не должна звучать ответом")

    def test_idle_restart_keeps_the_conversation_and_the_session(self) -> None:
        w = self.idle_w
        self.scenario_ok(w, "r_idle")
        st = w.state
        self.assertTrue(st["stopped_clean"])
        self.assertEqual(st["repeat_after_restart"], "Ответ r06.")
        self.assertEqual(w.voice["r07"][-1], "Ответ r07.")
        self.assertEqual(st["repeat_next"], "Ответ r07.")
        self.assertEqual(st["sessions_after"], st["sessions_before"], "рестарт не должен открывать новую сессию")

    def test_new_phrase_during_resumed_turn_does_not_reach_hermes(self) -> None:
        w = self.phrase_w
        self.scenario_ok(w, "r_phrase")
        st = w.state
        self.assertTrue(st["internal_started"], "дозапущенный ход не начался: сцена ничего не доказывает")
        self.assertIn(st["intruder"], harness.WAITING)
        self.assertNotIn("r05b", w.script.seen)
        self.assertFalse(st["asks"])
        self.assertEqual(st["reply"], RESUMED)
        self.assertEqual(len(st["finals"]), 1, "ответ не должен дублироваться")


if __name__ == "__main__":
    unittest.main()
