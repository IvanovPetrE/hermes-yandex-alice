"""Адаптер Алисы без Hermes (docs/PLAN.md, этап 6, срез «а»): заглушки модулей Hermes (`hermes_stub`) и настоящий loopback.

`aiohttp` нужен тестам (requirements-test.txt): без него модуль не загрузится и тесты упадут, а не будут пропущены.
Фикстуры синтетические; секрет пути, токены и реплики придуманы. Ожидаемые тексты Алисы — литералы (не константы
`protocol`): тест фиксирует то, что слышит владелец.
Негативные сценарии HTTP и жизненного цикла — по мотивам тестов alice-voice 0.3.2 (MIT, THIRD_PARTY_NOTICES.md), свои
тесты под модель жизненного цикла, которую ведёт Hermes.
"""
import asyncio
import contextlib
import json
import socket
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aiohttp  # noqa: E402  - нет пакета: тест падает, не пропускается
import hermes_stub as hs  # noqa: E402

adapter = hs.load_plugin()

SECRET, SKILL, OWNER, CHAT = "s3cr3t-path", "skill", "owner", "100500"
QUESTION = "расскажи про погоду в Лимасоле"


def alice_body(text=QUESTION, *, user=OWNER, skill=SKILL, session="s1", message=1, new=False) -> bytes:
    return json.dumps({
        "request": {"original_utterance": text, "command": text, "type": "SimpleUtterance"},
        "session": {"session_id": session, "message_id": message, "skill_id": skill, "new": new,
                    "user": {"user_id": user}},
        "version": "1.0",
    }, ensure_ascii=False).encode()


class FakeGateway:
    """Шлюз Hermes в миниатюре: `handle_message` запоминает событие и в фоне проигрывает ход по форме S1 — начало хода,
    (промежуточная отправка), итог с `reply_to` хода и `notify=True`, конец хода."""

    def __init__(self, answer="Готово, всё хорошо.", interim=None):
        self.events, self.answer, self.interim = [], answer, interim
        self._tasks = set()

    async def __call__(self, ad, event):
        self.events.append(event)
        task = asyncio.get_running_loop().create_task(self._turn(ad, event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _turn(self, ad, event):
        chat = event.source.chat_id
        await ad.on_processing_start(event)
        if self.interim:
            await ad.send(chat, self.interim, reply_to=event.message_id, metadata={"_interim_send": True, "notify": True})
        await ad.send(chat, self.answer, reply_to=event.message_id, metadata={"notify": True})
        await ad.on_processing_complete(event, None)

    async def close(self):
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


class AdapterCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        hs.world.reset()
        hs.world.scope.update({"ALICE_PATH_SECRET": SECRET, "ALICE_SKILL_ID": SKILL, "ALICE_OWNER_USER_ID": OWNER})
        self.port = hs.free_port()
        self.ctx = hs.FakeCtx()
        self.addCleanup(hs.world.close)

    def make(self, **extra):
        """Новый экземпляр адаптера из фабрики регистрации (как переподключение в Hermes)."""
        if not self.ctx.platform:
            adapter.register(self.ctx)
        config = hs.PlatformConfig(enabled=True, extra={"port": self.port, **extra})
        return self.ctx.platform["adapter_factory"](config)

    async def start(self, *, gateway=None, **extra):
        ad = self.make(**extra)
        self.gateway = gateway if gateway is not None else FakeGateway()
        hs.world.on_message = self.gateway
        self.assertTrue(await ad.connect())
        self.addAsyncCleanup(self.gateway.close)
        self.addAsyncCleanup(ad.disconnect)
        return ad

    def url(self, tail=SECRET) -> str:
        return f"http://127.0.0.1:{self.port}/{tail}"

    async def post(self, data: bytes, tail=SECRET):
        async with aiohttp.ClientSession() as session:
            async with session.post(self.url(tail), data=data) as resp:
                raw = await resp.read()
                return resp.status, (json.loads(raw) if raw else None)

    async def say(self, text, **kw):
        status, data = await self.post(alice_body(text, **kw))
        self.assertEqual(status, 200)
        return data["response"]["text"], data["response"]["end_session"]


class LifecycleTests(AdapterCase):
    def test_register_does_not_open_port(self):
        self.make()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", self.port))   # порт свободен: слушает только connect()
        self.assertEqual(self.ctx.platform["name"], "yandex_alice")
        self.assertIn("pre_tool_call", self.ctx.hooks)
        self.assertIn("telegram", self.ctx.handlers)

    async def test_connect_serves_ping_and_disconnect_frees_port(self):
        ad = await self.start()
        self.assertTrue(ad.is_connected())
        self.assertEqual(await self.say("ping"), ("pong", False))
        await ad.disconnect()
        self.assertFalse(ad.is_connected())
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", self.port))
        await ad.disconnect()   # повторный disconnect безопасен

    async def test_listens_on_loopback_only(self):
        """Слушатель на 0.0.0.0 принял бы соединение и на внешний адрес хоста; loopback-слушатель — нет. Внешний адрес
        берётся из таблицы маршрутов (UDP connect ничего не отправляет); у хоста без сети проверка ничего не доказывает."""
        await self.start()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            try:
                probe.connect(("192.0.2.1", 9))
                external = probe.getsockname()[0]
            except OSError:
                external = None
        if external and not external.startswith("127."):
            with socket.socket() as sock:
                sock.settimeout(1)
                code = await asyncio.to_thread(sock.connect_ex, (external, self.port))
            self.assertNotEqual(code, 0)
        with socket.socket() as sock:
            self.assertEqual(sock.connect_ex(("127.0.0.1", self.port)), 0)

    async def test_reconnect_is_a_new_instance_on_same_port(self):
        first = await self.start()
        await first.disconnect()
        second = self.make()
        self.assertIsNot(first, second)
        self.assertTrue(await second.connect(is_reconnect=True))
        self.addAsyncCleanup(second.disconnect)
        self.assertEqual(await self.say("ping"), ("pong", False))

    async def test_busy_port_is_retryable_fatal_error(self):
        ad = self.make()
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", self.port))
            busy.listen()
            self.assertFalse(await ad.connect())
        self.assertEqual((ad.fatal_error_code, ad.fatal_error_retryable), ("bind_failed", True))
        self.assertNotIn(SECRET, ad.fatal_error_message)
        self.assertNotIn(str(self.port), ad.fatal_error_message)

    async def test_missing_secret_is_permanent_fatal_error(self):
        del hs.world.scope["ALICE_PATH_SECRET"]
        ad = self.make()
        self.assertFalse(await ad.connect())
        self.assertEqual((ad.fatal_error_code, ad.fatal_error_retryable), ("config_missing", False))

    async def test_invalid_port_is_permanent_fatal_error(self):
        for port in (None, "x", 0, 70000):
            with self.subTest(port=port):
                ad = self.make(port=port)
                self.assertFalse(await ad.connect())
                self.assertEqual((ad.fatal_error_code, ad.fatal_error_retryable), ("config_invalid", False))

    async def test_unavailable_state_storage_keeps_platform_working(self):
        hs.world.db_error = RuntimeError("boom")
        with self.assertLogs("hya_plugin", level="WARNING") as logs:
            await self.start()
        self.assertEqual(logs.output, ["WARNING:hya_plugin.adapter:state storage unavailable: RuntimeError"])
        self.assertEqual(await self.say(QUESTION), ("Готово, всё хорошо.", False))


class ConnectionLimitTests(AdapterCase):
    """HERMESYAND-6: не более 20 одновременных соединений; лишнее закрывается сразу, без ответа."""

    async def test_the_21st_connection_is_closed_and_a_freed_slot_is_reusable(self) -> None:
        await self.start()
        held = []
        for _ in range(20):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            held.append((reader, writer))
            self.addCleanup(writer.close)
        with self.assertLogs("hya_plugin.listener", level="WARNING") as logs:
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            self.addCleanup(writer.close)
            try:   # лишнее соединение закрыто сервером без ответа
                data = await asyncio.wait_for(reader.read(1), 2)
            except ConnectionResetError:
                data = b""
        self.assertEqual(data, b"")
        self.assertEqual(logs.output, ["WARNING:hya_plugin.listener:connection limit reached: rejected=1"])
        self.assertNotIn(str(self.port), logs.output[0])
        held[0][1].close()   # освободили один слот: новое соединение принимается и обслуживается
        await held[0][1].wait_closed()
        for _ in range(50):   # сервер узнаёт о закрытии асинхронно
            try:
                if await self.say("ping") == ("pong", False):
                    return
            except (aiohttp.ClientError, ConnectionError):
                await asyncio.sleep(0.02)
        self.fail("слот не освободился после закрытия соединения")


class NegativeHttpTests(AdapterCase):
    async def test_wrong_path_is_404(self):
        await self.start()
        for tail in ("wrong", "", SECRET + "x", SECRET[:-1], SECRET.upper(), SECRET + "/"):
            with self.subTest(tail=tail):
                async with aiohttp.ClientSession() as session:
                    async with session.post(self.url(tail), data=alice_body()) as resp:
                        self.assertEqual(resp.status, 404)

    async def test_wrong_path_is_checked_before_body_size(self):
        await self.start()
        async with aiohttp.ClientSession() as session:
            async with session.post(self.url("wrong"), data=b"x" * (64 * 1024)) as resp:
                self.assertEqual(resp.status, 404)

    async def test_get_is_405(self):
        await self.start()
        async with aiohttp.ClientSession() as session:
            async with session.get(self.url()) as resp:
                self.assertEqual(resp.status, 405)

    async def test_body_over_limit_is_413(self):
        await self.start()
        status, _ = await self.post(b" " * (16 * 1024 + 1))
        self.assertEqual(status, 413)

    async def test_slow_body_is_408(self):
        await self.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            with mock.patch.object(adapter, "READ_TIMEOUT", 0.2):
                writer.write(f"POST /{SECRET} HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n{{".encode())
                await writer.drain()
                line = await asyncio.wait_for(reader.readline(), 3)
        finally:
            writer.close()
        self.assertTrue(line.startswith(b"HTTP/1.1 408"), line)

    async def test_bad_bodies_are_400(self):
        await self.start()
        bad = {
            "empty": b"",
            "not json": b"{",
            "not an object": b"[]",
            "no session": b"{}",
            "message_id is bool": json.dumps({"session": {"session_id": "s", "message_id": True}}).encode(),
            "text is number": json.dumps({"session": {}, "request": {"original_utterance": 5}}).encode(),
            "new is string": json.dumps({"session": {"new": "yes"}}).encode(),
        }
        for name, data in bad.items():
            with self.subTest(name):
                status, _ = await self.post(data)
                self.assertEqual(status, 400)

    async def test_nothing_reaches_agent_on_negative_requests(self):
        await self.start()
        await self.post(b"{")
        await self.post(alice_body(), tail="wrong")
        await self.say("ping")
        self.assertEqual(self.gateway.events, [])


class EntryTests(AdapterCase):
    async def test_foreign_skill_and_user_get_owner_only_refusal(self):
        await self.start()
        cases = {"skill": {"skill": "other"}, "user": {"user": "stranger"},
                 "stranger, empty text": {"text": "", "user": "stranger"}}
        for n, (name, kw) in enumerate(cases.items(), 1):   # разные message_id: повтор хода дедуплицируется
            with self.subTest(name):
                kw = {"text": QUESTION, **kw, "message": n}
                self.assertEqual(await self.say(**kw), ("Этот навык работает только для владельца.", True))
        self.assertEqual(self.gateway.events, [])

    async def test_unbound_mode_refuses_everyone_and_logs_user_id_once(self):
        del hs.world.scope["ALICE_OWNER_USER_ID"]
        with self.assertLogs("hya_plugin", level="WARNING") as logs:
            await self.start()
            for n in (1, 2):
                self.assertEqual(await self.say(QUESTION, user="stranger", message=n),
                                 ("Навык ещё не привязан к владельцу.", True))
        self.assertEqual(sum("stranger" in line for line in logs.output), 1)
        self.assertEqual(self.gateway.events, [])

    async def test_rejected_commands_are_not_passed_to_agent(self):
        await self.start()
        commands = {"slash": "/status", "control char": "a\x07b", "too long": "а" * 1001}
        for n, (name, text) in enumerate(commands.items(), 1):
            with self.subTest(name):
                self.assertEqual(await self.say(text, message=n), ("Такую команду я не передам.", False))
        self.assertEqual(self.gateway.events, [])

    async def test_ping_is_case_insensitive_and_skips_agent(self):
        await self.start()
        self.assertEqual(await self.say("PING"), ("pong", False))
        self.assertEqual(self.gateway.events, [])


class QuestionTests(AdapterCase):
    async def test_final_answer_goes_to_alice_and_session_stays_open(self):
        await self.start()
        self.assertEqual(await self.say(QUESTION), ("Готово, всё хорошо.", False))

    async def test_message_to_hermes_is_an_owner_dm_without_gateway_control(self):
        await self.start()
        await self.say(QUESTION)
        (event,) = self.gateway.events
        self.assertEqual(event.text, QUESTION)
        self.assertIs(event.allow_gateway_control, False)
        self.assertFalse(event.internal)
        self.assertEqual((event.source.chat_type, event.source.user_id), ("dm", OWNER))
        self.assertTrue(event.source.chat_id.startswith("alice-"))

    async def test_interim_send_is_not_the_voice_answer(self):
        await self.start(gateway=FakeGateway(interim="⏳ Ещё работаю над этим"))
        self.assertEqual(await self.say(QUESTION), ("Готово, всё хорошо.", False))

    async def test_yandex_retry_gets_same_answer_without_second_turn(self):
        await self.start()
        first = await self.say(QUESTION, message=7)
        again = await self.say(QUESTION, message=7)
        self.assertEqual(again, first)
        self.assertEqual(len(self.gateway.events), 1)

    async def test_logs_have_no_secret_path_or_reply_text(self):
        with self.assertLogs("hya_plugin", level="DEBUG") as logs:
            await self.start(gateway=FakeGateway(interim="⏳ Ещё работаю над этим"))
            await self.say(QUESTION)
            await self.post(b"{")
            await self.post(alice_body(), tail="wrong")
            await self.say("/status", message=2)
        text = "\n".join(logs.output)
        for forbidden in (SECRET, QUESTION, "Готово, всё хорошо.", "Ещё работаю", "/status"):
            self.assertNotIn(forbidden, text)


class HoldGateway:
    """Шлюз открывает ход и ничего не отвечает: итог и конец хода тест подаёт сам."""

    def __init__(self):
        self.events = []

    async def __call__(self, ad, event):
        self.events.append(event)
        await ad.on_processing_start(event)

    async def close(self):
        return None


class RestartTests(AdapterCase):
    async def test_turn_ended_by_gateway_shutdown_keeps_the_question(self):
        # compat (группа R): остановка шлюза завершает ход без итога; адаптер не говорит «не получилось» —
        # Hermes дозапустит ход после старта
        ad = await self.start(gateway=HoldGateway())
        self.assertEqual((await self.say(QUESTION))[0], "Вопрос принят. Спросите ответ позже или готовы подождать?")
        (event,) = self.gateway.events
        ad.gateway_runner = SimpleNamespace(_draining=True)
        await ad.on_processing_complete(event, None)
        self.assertEqual((await self.say("да", message=2))[0], "Ещё не готово. Подождёте?")

    async def test_turn_ended_without_a_final_outside_shutdown_still_fails(self):
        ad = await self.start(gateway=HoldGateway())
        await self.say(QUESTION)
        (event,) = self.gateway.events
        ad.gateway_runner = SimpleNamespace(_draining=False)
        await ad.on_processing_complete(event, None)
        self.assertEqual((await self.say("да", message=2))[0], "Не получилось, попробуйте ещё раз.")

    async def test_final_of_a_turn_opened_before_reconnect_is_the_voice_answer(self):
        # PLAN, этап 5 («Замечено»): ход открыл прежний экземпляр, итог пришёл новому — это итог, а не отправка вне хода
        first = await self.start(gateway=HoldGateway())
        await self.say(QUESTION)
        (event,) = self.gateway.events
        chat = event.source.chat_id
        await first.disconnect()
        second = self.make()
        self.assertTrue(await second.connect(is_reconnect=True))
        self.addAsyncCleanup(second.disconnect)
        await second.send(chat, "Итог после переподключения.", reply_to=event.message_id, metadata={"notify": True})
        await first.on_processing_complete(event, None)   # поздний конец хода у прежнего экземпляра ничего не ломает
        self.assertEqual(await self.say("да", message=2), ("Итог после переподключения.", False))


class LostTurnTests(AdapterCase):
    """HERMESYAND-3: ход без on_processing_complete снимается по сроку в обоих местах; его поздний итог — в Telegram."""

    def telegram(self) -> list:
        sent = []

        async def sender(pconfig, chat, text, notify=False):
            sent.append(text)
            return {"success": True}

        hs.world.sender = sender
        hs.world.scope.update({"TELEGRAM_BOT_TOKEN": "0:test-token", "ALICE_TELEGRAM_CHAT": CHAT})
        return sent

    @staticmethod
    def later(ad, seconds: float) -> None:
        real = ad._engine._wall
        ad._engine._wall = lambda: real() + seconds

    async def test_the_deadline_drops_the_adapter_record_and_a_late_final_goes_to_telegram(self):
        sent = self.telegram()
        ad = await self.start(gateway=HoldGateway())
        await self.say(QUESTION)
        (event,) = self.gateway.events
        chat = event.source.chat_id
        self.assertIn(chat, ad._open)
        self.later(ad, 901)
        ad._engine.tick()
        self.assertNotIn(chat, ad._open)
        self.assertTrue(await hs.eventually(lambda: "Алиса:\nПрошлый ответ не пришёл." in sent))
        await ad.send(chat, "Поздний итог.", reply_to=event.message_id, metadata={"notify": True})
        self.assertTrue(await hs.eventually(lambda: "Алиса:\nПоздний итог." in sent))
        self.assertIsNone(ad._engine.state.answer)   # голосом не звучит

    async def test_a_late_final_while_a_new_turn_is_open_goes_to_telegram_not_to_the_new_question(self):
        sent = self.telegram()
        ad = await self.start(gateway=HoldGateway())
        await self.say(QUESTION)
        old = self.gateway.events[0]
        self.later(ad, 901)
        ad._engine.tick()
        self.assertEqual((await self.say("а завтра?", message=2))[0],
                         "Вопрос принят. Спросите ответ позже или готовы подождать?")
        new = self.gateway.events[1]
        await ad.send(old.source.chat_id, "Поздний итог.", reply_to=old.message_id, metadata={"notify": True})
        self.assertTrue(await hs.eventually(lambda: "Алиса:\nПоздний итог." in sent))
        self.assertIsNotNone(ad._engine.state.question)   # новый вопрос в работе
        await ad.on_processing_complete(old, None)         # поздний complete снятого хода
        self.assertIsNotNone(ad._engine.state.question)
        self.assertIn(new.source.chat_id, ad._open)

    async def test_an_interim_send_in_the_open_turn_is_a_sign_of_life(self):
        ad = await self.start(gateway=HoldGateway())
        await self.say(QUESTION)
        (event,) = self.gateway.events
        started = ad._engine.state.question.alive_at
        self.later(ad, 600)
        await ad.send(event.source.chat_id, "⏳ Ещё работаю", reply_to=event.message_id,
                      metadata={"_interim_send": True, "notify": True})
        self.assertGreaterEqual(ad._engine.state.question.alive_at, started + 600)


class RecoveredSendTests(AdapterCase):
    """HERMESYAND-4: пометка «♻️ …:» у отправки вне хода — восстановленный ответ только при вопросе в работе и
    перезапуске шлюза после него; иначе это обычная отправка вне хода (Telegram, вопрос не закрыт)."""

    MARKED = "♻️ Чужая рассылка:\n\nТекст рассылки."

    telegram = LostTurnTests.telegram

    async def test_marked_send_without_a_restart_goes_to_telegram_and_keeps_the_question(self):
        sent = self.telegram()
        ad = await self.start(gateway=HoldGateway())
        await self.say(QUESTION)
        (event,) = self.gateway.events
        chat = event.source.chat_id
        ad.gateway_runner = SimpleNamespace(_draining=True)
        await ad.on_processing_complete(event, None)   # хода нет, вопрос в работе, перезапуска не было
        await ad.send(chat, self.MARKED)
        self.assertTrue(await hs.eventually(lambda: "Алиса:\n" + self.MARKED in sent))
        self.assertIsNotNone(ad._engine.state.question)
        self.assertIsNone(ad._engine.state.answer)

    async def test_marked_send_after_a_restart_is_the_voice_answer(self):
        first = await self.start(gateway=HoldGateway())
        await self.say(QUESTION)
        (event,) = self.gateway.events
        chat = event.source.chat_id
        await first.disconnect()
        second = self.make()
        self.assertTrue(await second.connect(is_reconnect=True))   # шлюз стартовал при вопросе в работе
        self.addAsyncCleanup(second.disconnect)
        second._drop_turn(chat)   # ответ из леджера приходит вне хода
        await second.send(chat, self.MARKED)
        self.assertEqual(await self.say("да", message=2), ("Текст рассылки.", False))


class SendTests(AdapterCase):
    async def test_send_outside_turn_without_telegram_is_accepted_and_silent(self):
        ad = await self.start()
        result = await ad.send(CHAT, "напоминание из cron")
        self.assertTrue(result.success)

    async def test_full_command_block_is_refused(self):
        ad = await self.start()
        block = "```\n" + "x" * 3100 + "\n```"
        result = await ad.send(CHAT, block, metadata={"_interim_send": True})
        self.assertFalse(result.success)

    async def test_outside_turn_goes_to_telegram_with_alice_label(self):
        sent = []

        async def sender(pconfig, chat, text, notify=False):
            sent.append((pconfig.token, chat, text, notify))
            return {"success": True}

        hs.world.sender = sender
        hs.world.scope.update({"TELEGRAM_BOT_TOKEN": "0:test-token", "ALICE_TELEGRAM_CHAT": CHAT})
        ad = await self.start()
        await ad.send("alice-x", "Привет из cron")
        self.assertTrue(await hs.eventually(lambda: sent))
        self.assertEqual(sent, [("0:test-token", CHAT, "Алиса:\nПривет из cron", True)])


class ValidateConfigTests(AdapterCase):
    def test_secret_in_scope_is_required(self):
        self.assertTrue(adapter.validate_config(None))
        del hs.world.scope["ALICE_PATH_SECRET"]
        self.assertFalse(adapter.validate_config(None))

    def test_telegram_chat_must_fit_cron_address(self):
        for chat, ok in (("12345", True), ("a:b", False), ("a b", False), ("a,b", False)):
            with self.subTest(chat=chat):
                hs.world.scope["ALICE_TELEGRAM_CHAT"] = chat
                with contextlib.nullcontext() if ok else self.assertLogs("hya_plugin", level="WARNING"):
                    self.assertIs(adapter.validate_config(None), ok)


if __name__ == "__main__":
    unittest.main()
