"""Подтверждения опасных команд через карточку в Telegram (docs/DESIGN.md, этап 5, срез «б»; docs/RECON.md, «S4») —
без Hermes, PTB и сети: бот и Hermes — фейки, часы — инъекцией."""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "yandex-alice"))
import approvals  # noqa: E402


class CardTextTests(unittest.TestCase):
    """Текст карточки — HTML для `parse_mode=HTML`; заголовок — решение владельца 2026-11-17."""

    def test_card_shows_title_escaped_command_and_reason(self) -> None:
        text, whole = approvals.render_card("rm -r <dir> && echo 'ok'", "удаляет & чистит", smart_denied=False)
        self.assertTrue(text.startswith("⚠️ <b>Алиса: подтвердите команду</b>\n"))
        self.assertIn("<pre>rm -r &lt;dir&gt; &amp;&amp; echo &#x27;ok&#x27;</pre>", text)
        self.assertIn("Причина: удаляет &amp; чистит", text)
        self.assertNotIn("<dir>", text)
        self.assertTrue(whole)

    def test_card_with_any_final_line_fits_telegram_limit(self) -> None:
        """Telegram считает 4096 в единицах UTF-16 по готовому тексту; иначе «message is too long» — автоотказ, а
        правка итога не проходит и кнопки остаются (ревью S4, п. 10)."""
        def units(text: str) -> int:
            return len(text.encode("utf-16-le")) // 2

        cases = {
            "ampersands": ("&" * 5000, "&" * 5000),
            "emoji": ("🙂" * 5000, "🙂" * 5000),
            "plain long": ("x" * 10000, "y" * 10000),
            "quotes": ('"' * 3499, "<" * 499),
        }
        for name, (command, reason) in cases.items():
            for smart in (False, True):
                with self.subTest(name, smart=smart):
                    text, _whole = approvals.render_card(command, reason, smart_denied=smart)
                    for line in approvals.FINAL_LINES:
                        self.assertLessEqual(units(text + "\n\n" + line), 4096)

    def test_long_reason_gives_way_before_the_command(self) -> None:
        command = "echo " + "a" * 3000
        text, whole = approvals.render_card(command, "&" * 5000, smart_denied=True)
        self.assertTrue(whole)
        self.assertIn(f"<pre>{command}</pre>", text)
        self.assertIn("Причина: &amp;", text)


class ChoicesTests(unittest.TestCase):
    """Только одноразовое разрешение; «Разрешить» — только если команда видна целиком; «Отклонить» — всегда (S4)."""

    ACTIONS = [("Allow once", "once", "primary"), ("Allow session", "session", ""), ("Always", "always", ""),
               ("Deny", "deny", "danger")]

    def test_card_offers_only_once_and_deny(self) -> None:
        cases = {
            "all tiers, whole": (self.ACTIONS, True, ("once", "deny")),
            "all tiers, cut": (self.ACTIONS, False, ("deny",)),
            "hermes deny-only": ([("Deny", "deny", "danger")], True, ("deny",)),
            "deny missing": ([("Allow once", "once", "primary")], True, ("once", "deny")),
            "nothing": ([], True, ("deny",)),
        }
        for name, (actions, whole, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(approvals.offered_choices(actions, whole), expected)


class CallbackDataTests(unittest.TestCase):
    """`callback_data` Telegram — не больше 64 байт; чужие кнопки (`ea:` ядра и прочие) не наши."""

    def test_our_button_round_trips_within_64_bytes(self) -> None:
        data = approvals.callback_data("once", "tok_EN-123456789")
        self.assertLessEqual(len(data.encode()), 64)
        self.assertTrue(data.startswith("hya:"))
        self.assertEqual(approvals.parse_callback(data), ("once", "tok_EN-123456789"))

    def test_foreign_or_broken_data_is_not_ours(self) -> None:
        for data in ("", "ea:once:1", "hya:once:", "hya:once", "hya:once:a:b", None):
            with self.subTest(data=data):
                self.assertIsNone(approvals.parse_callback(data))


class FullCommandBlockTests(unittest.TestCase):
    """Перед урезанной карточкой Hermes шлёт всю команду через голосовой `send()` блоком ```` ``` ```` с `_interim_send`
    (`base_exec_approval.send_full_command`). Владелец его не видит — `send()` отказывает, и Hermes оставляет «только
    отклонить» (S4, п. 5)."""

    def test_only_a_long_interim_fenced_block_is_refused(self) -> None:
        long_block = "```\n" + "x" * 3001 + "\n```"
        cases = {
            "long interim block": (long_block, {"_interim_send": True}, True),
            "not interim": (long_block, {}, False),
            "no metadata": (long_block, None, False),
            "short block": ("```\nls\n```", {"_interim_send": True}, False),
            "not a block": ("x" * 4000, {"_interim_send": True}, False),
            "empty": ("", {"_interim_send": True}, False),
        }
        for name, (content, meta, expected) in cases.items():
            with self.subTest(name):
                self.assertIs(approvals.is_full_command_block(content, meta), expected)


class OwnerTests(unittest.TestCase):
    """Кто нажимает (S4, вывод 2): `ALICE_TELEGRAM_OWNER_ID`, иначе id лички `ALICE_TELEGRAM_CHAT` (в личке chat_id ==
    user_id); группа без владельца и мусор — подтверждения отключены (владелец None)."""

    def test_owner_is_resolved_from_chat_and_setting(self) -> None:
        cases = {
            "private chat": ("100", "", (100, 100)),
            "group without owner": ("-1001", "", (-1001, None)),
            "group with owner": ("-1001", "100", (-1001, 100)),
            "explicit owner wins in private": ("100", "200", (100, 200)),
            "spaces": (" 100 ", " ", (100, 100)),
            "owner garbage": ("100", "abc", (100, None)),
            "chat garbage": ("@chan", "100", (None, None)),
            "no chat": ("", "100", (None, None)),
        }
        for name, (chat, owner, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(approvals.resolve_owner(chat, owner), expected)


def make_book(now=None) -> "approvals.Book":
    now = now if now is not None else [0.0]
    tokens, ids = iter(f"tok{i}" for i in range(1, 100)), iter(f"hya-card:{i}" for i in range(1, 100))
    return approvals.Book(clock=lambda: now[0], new_token=lambda: next(tokens), new_card_id=lambda: next(ids))


CHAT, OWNER = 100, 100   # синтетика: личка владельца


class BookTests(unittest.TestCase):
    """Книга карточек (перенос `prototypes/s4/plugin/approval_book.py`): `sending → open → resolving →
    resolved/failed/expired`, каждый переход под одним замком."""

    def test_no_card_without_request_id_or_session(self) -> None:
        """`resolve_*` без `request_id` ответил бы на самый старый запрос сессии — карточки нет."""
        book = make_book()
        for sk, rid in (("agent:p:yandex_alice:dm:c1", None), ("agent:p:yandex_alice:dm:c1", ""), ("", "r1")):
            with self.subTest(session_key=sk, request_id=rid):
                self.assertIsNone(book.open(sk, rid, "c1", ("once", "deny"), "text"))
        self.assertFalse(book.awaiting("c1"))

    def test_press_checks_chat_then_owner_then_state_then_choice(self) -> None:
        book = make_book()
        card = book.open("sk", "r1", "c1", ("deny",), "text")
        press = lambda user, chat, choice="deny": book.press(card.token, choice, user, chat, chat=CHAT, owner=OWNER)
        self.assertEqual(press(OWNER, 555)[0], approvals.WRONG_CHAT)
        self.assertEqual(press(777, CHAT)[0], approvals.NOT_OWNER)
        self.assertEqual(press(OWNER, CHAT)[0], approvals.WAIT)          # отправка ещё не подтверждена Telegram
        self.assertTrue(book.mark_sent(card.card_id, 42))
        self.assertEqual(press(OWNER, CHAT, "once")[0], approvals.NOT_OFFERED)
        self.assertEqual(press(OWNER, CHAT), (approvals.RESOLVE, card))
        self.assertEqual(press(OWNER, CHAT)[0], approvals.STALE)        # второе нажатие: ровно одно побеждает
        self.assertEqual(book.press("nope", "deny", OWNER, CHAT, chat=CHAT, owner=OWNER), (approvals.STALE, None))

    def test_stranger_learns_nothing_about_the_card(self) -> None:
        book = make_book()
        card = book.open("sk", "r1", "c1", ("once", "deny"), "text")
        for user, chat in ((777, CHAT), ("x", CHAT), (None, CHAT), (OWNER, None)):
            with self.subTest(user=user, chat=chat):
                decision, _ = book.press(card.token, "once", user, chat, chat=CHAT, owner=OWNER)
                self.assertIn(decision, (approvals.NOT_OWNER, approvals.WRONG_CHAT))

    def test_disabled_approvals_refuse_everyone(self) -> None:
        book = make_book()
        card = book.open("sk", "r1", "c1", ("once", "deny"), "text")
        book.mark_sent(card.card_id, 42)
        decision, _ = book.press(card.token, "once", -1001, -1001, chat=-1001, owner=None)
        self.assertEqual(decision, approvals.NOT_OWNER)

    def test_failed_card_cannot_be_sent_and_sent_cannot_fail(self) -> None:
        book = make_book()
        a = book.open("sk", "r1", "c1", ("deny",), "t")
        self.assertTrue(book.fail(a.card_id))
        self.assertFalse(book.mark_sent(a.card_id, 1))    # поздняя доставка после срока
        self.assertEqual(a.state, "failed")
        b = book.open("sk", "r2", "c1", ("deny",), "t")
        self.assertTrue(book.mark_sent(b.card_id, 2))
        self.assertFalse(book.fail(b.card_id))            # срок после доставки — уже не сбой
        self.assertEqual(b.state, "open")

    def test_resolve_that_found_nothing_is_expired_not_failed(self) -> None:
        book = make_book()
        card = book.open("sk", "r1", "c1", ("deny",), "t")
        book.mark_sent(card.card_id, 1)
        book.press(card.token, "deny", OWNER, CHAT, chat=CHAT, owner=OWNER)
        book.finish(card.card_id, 0)
        self.assertEqual(card.state, "expired")
        other = book.open("sk", "r2", "c1", ("deny",), "t")
        book.mark_sent(other.card_id, 2)
        book.press(other.token, "deny", OWNER, CHAT, chat=CHAT, owner=OWNER)
        book.finish(other.card_id, 1)
        self.assertEqual(other.state, "resolved")

    def test_awaiting_follows_live_cards_of_the_chat(self) -> None:
        book = make_book()
        card = book.open("sk", "r1", "c1", ("deny",), "t")
        timeline = [book.awaiting("c1")]
        book.mark_sent(card.card_id, 1)
        timeline.append(book.awaiting("c1"))
        book.press(card.token, "deny", OWNER, CHAT, chat=CHAT, owner=OWNER)
        timeline.append(book.awaiting("c1"))
        book.finish(card.card_id, 1)
        timeline.append(book.awaiting("c1"))
        self.assertEqual(timeline, [True, True, True, False])
        self.assertFalse(book.awaiting("other"))

    def test_only_an_open_card_expires(self) -> None:
        book = make_book()
        sending = book.open("sk", "r1", "c1", ("deny",), "t")
        self.assertIsNone(book.expire(sending.card_id))
        book.mark_sent(sending.card_id, 1)
        self.assertIs(book.expire(sending.card_id), sending)
        self.assertIsNone(book.expire(sending.card_id))
        self.assertIsNone(book.expire("hya-card:unknown"))

    def test_closing_a_chat_expires_its_open_cards_only(self) -> None:
        book = make_book()
        mine, other = book.open("sk", "r1", "c1", ("deny",), "t"), book.open("sk2", "r2", "c2", ("deny",), "t")
        pending = book.open("sk", "r3", "c1", ("deny",), "t")
        book.mark_sent(mine.card_id, 1)
        book.mark_sent(other.card_id, 2)
        self.assertEqual(book.close_chat("c1"), [mine])
        self.assertEqual((mine.state, other.state, pending.state), ("expired", "open", "sending"))

    def test_finished_cards_are_forgotten_after_a_day_live_ones_are_kept(self) -> None:
        now = [0.0]
        book = make_book(now)
        done, live = book.open("sk", "r1", "c1", ("deny",), "t"), book.open("sk", "r2", "c1", ("deny",), "t")
        book.fail(done.card_id)
        book.mark_sent(live.card_id, 1)
        now[0] = 86400.0
        book.open("sk", "r3", "c1", ("deny",), "t")   # уборка — при новой карточке
        self.assertEqual(book.press(done.token, "deny", OWNER, CHAT, chat=CHAT, owner=OWNER), (approvals.STALE, None))
        self.assertEqual(book.press(live.token, "deny", OWNER, CHAT, chat=CHAT, owner=OWNER)[0], approvals.RESOLVE)


# --- оркестратор: бот и Hermes — фейки -------------------------------------------------------------------------------

COMMAND, REASON = "rm -r /srv/marker-cmd-7f3a", "marker-reason-9c1e"
ALL_ACTIONS = [("Allow once", "once", "primary"), ("Session", "session", ""), ("Deny", "deny", "danger")]


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, tuple]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.gate: asyncio.Future | None = None   # задан — отправка ждёт его
        self.error: Exception | None = None
        self.edit_errors: list[Exception] = []   # очередь сбоев правки: каждая правка снимает по одному
        self.next_id = 41

    async def send(self, chat: int, text: str, buttons: tuple) -> int:
        self.sent.append((chat, text, buttons))
        if self.gate is not None:
            await self.gate
        if self.error is not None:
            raise self.error
        self.next_id += 1
        return self.next_id

    async def edit(self, chat: int, message_id: int, text: str) -> None:
        if self.edit_errors:
            raise self.edit_errors.pop(0)
        self.edits.append((chat, message_id, text))


class FakeHermes:
    def __init__(self) -> None:
        self.denied: list[tuple[str, str, str]] = []
        self.resolved: list[tuple[str, str, str]] = []
        self.count = 1

    def deny(self, session_key: str, request_id: str, reason: str) -> int:
        self.denied.append((session_key, request_id, reason))
        return 1

    def resolve(self, session_key: str, choice: str, request_id: str) -> int:
        self.resolved.append((session_key, choice, request_id))
        return self.count


class ApprovalsTestCase(unittest.IsolatedAsyncioTestCase):
    def make(self, chat="100", owner="", enabled=True, bot=True, deadline=10.0) -> "approvals.Approvals":
        self.bot, self.hermes, self.waiting, self.logs = FakeBot(), FakeHermes(), [], []
        appr = approvals.Approvals(self.hermes.deny, self.hermes.resolve, bot=lambda: self.bot if bot else None,
                                   deadline=deadline, book=make_book(), log=self.logs.append)
        appr.configure(chat, owner, enabled=enabled, notify=lambda c, w: self.waiting.append((c, w)))
        return appr

    async def prompt(self, appr, actions=ALL_ACTIONS, request_id="r1", command=COMMAND):
        return await appr.prompt(alice_chat="c1", session_key="sk", request_id=request_id, command=command,
                                 description=REASON, smart_denied=False, actions=actions)


class PromptTests(ApprovalsTestCase):
    async def test_disabled_approvals_deny_at_once_without_telegram(self) -> None:
        for name, kwargs in {"no telegram": {"enabled": False}, "group without owner": {"chat": "-1001"},
                             "no chat": {"chat": ""}}.items():
            with self.subTest(name):
                appr = self.make(**kwargs)
                self.assertIsNone(await self.prompt(appr))
                self.assertEqual(self.bot.sent, [])
                self.assertEqual(self.hermes.denied, [("sk", "r1", approvals.DENY_OFF)])
                self.assertEqual(appr.outcome("c1"), "off")
                self.assertFalse(appr.awaiting("c1"))

    async def test_no_bot_is_a_failure_not_disabled_approvals(self) -> None:
        """Решение владельца 2026-11-17: чат и токен заданы, а бота профиля по умолчанию нет — как сбой."""
        appr = self.make(bot=False)
        self.assertIsNone(await self.prompt(appr))
        self.assertEqual(self.hermes.denied, [("sk", "r1", approvals.DENY_FAILED)])
        self.assertEqual(appr.outcome("c1"), "failed")
        self.assertFalse(appr.awaiting("c1"))

    async def test_card_goes_to_the_configured_chat_with_once_and_deny(self) -> None:
        appr = self.make(chat="100")
        card_id = await self.prompt(appr)
        self.assertTrue(card_id.startswith("hya-card:"))
        [(chat, text, buttons)] = self.bot.sent
        self.assertEqual(chat, 100)
        self.assertIn("rm -r /srv/marker-cmd-7f3a", text)
        self.assertEqual([label for label, _data in buttons], ["Разрешить один раз", "Отклонить"])
        self.assertEqual([approvals.parse_callback(data)[0] for _label, data in buttons], ["once", "deny"])
        self.assertEqual(self.hermes.denied, [])
        self.assertTrue(appr.awaiting("c1"))
        self.assertEqual(self.waiting[-1], ("c1", True))
        self.assertEqual(appr.outcome("c1"), "")

    async def test_send_error_denies_at_once(self) -> None:
        appr = self.make()
        self.bot.error = RuntimeError("Bad Request: chat not found")
        self.assertIsNone(await self.prompt(appr))
        self.assertEqual(self.hermes.denied, [("sk", "r1", approvals.DENY_FAILED)])
        self.assertEqual(appr.outcome("c1"), "failed")
        self.assertFalse(appr.awaiting("c1"))
        self.assertEqual(self.waiting[-1], ("c1", False))

    async def test_send_past_deadline_denies_and_a_late_card_turns_stale(self) -> None:
        """Срок < 15 с, после которых Hermes считает исход неясным и держит запрос до таймаута (S4, вывод 4)."""
        appr = self.make(deadline=0.05)
        self.bot.gate = asyncio.get_running_loop().create_future()
        loop = asyncio.get_running_loop()
        started = loop.time()
        self.assertIsNone(await self.prompt(appr))
        self.assertGreaterEqual(loop.time() - started, 0.05)
        self.assertEqual(self.hermes.denied, [("sk", "r1", approvals.DENY_FAILED)])
        self.assertEqual(appr.outcome("c1"), "failed")
        self.bot.gate.set_result(None)   # Telegram всё-таки принял карточку
        await settle()
        [(chat, message_id, text)] = self.bot.edits
        self.assertEqual((chat, message_id), (100, 42))
        self.assertTrue(text.endswith("\n\n⌛ Запрос неактуален"))
        self.assertEqual(len(self.hermes.denied), 1)

    async def test_cancelled_turn_during_send_denies_instead_of_hanging_in_sending(self) -> None:
        """`CancelledError` — не `Exception`: без явной обработки карточка навсегда в `sending` (ревью S4, п. 10)."""
        appr = self.make()
        self.bot.gate = asyncio.get_running_loop().create_future()
        task = asyncio.ensure_future(self.prompt(appr))
        await settle()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.hermes.denied, [("sk", "r1", approvals.DENY_FAILED)])
        self.assertEqual(appr.outcome("c1"), "failed")
        self.assertFalse(appr.awaiting("c1"))


class PressTests(ApprovalsTestCase):
    async def open_card(self, appr):
        await self.prompt(appr)
        [(_chat, _text, buttons)] = self.bot.sent
        return {approvals.parse_callback(data)[0]: data for _label, data in buttons}

    async def test_owner_press_settles_this_request_and_shows_the_result(self) -> None:
        for choice, line in (("once", "✅ Разрешено один раз"), ("deny", "❌ Отклонено")):
            with self.subTest(choice):
                appr = self.make()
                data = await self.open_card(appr)
                reply = await appr.press(data[choice], OWNER, CHAT)
                self.assertEqual(self.hermes.resolved, [("sk", choice, "r1")])
                self.assertEqual(reply.toast, line)
                self.assertFalse(reply.alert)
                [(_chat, message_id, text)] = self.bot.edits
                self.assertEqual(message_id, 42)
                self.assertTrue(text.endswith("\n\n" + line))
                self.assertFalse(appr.awaiting("c1"))
                self.assertEqual(self.waiting[-1], ("c1", False))

    async def test_refused_presses_resolve_nothing_and_leave_the_card(self) -> None:
        appr = self.make()
        data = await self.open_card(appr)
        forged = approvals.parse_callback(data["deny"])[1]
        cases = {
            "stranger": (data["once"], 777, CHAT, "Отвечать может только владелец", True),
            "other chat": (data["once"], OWNER, 555, "Отвечать может только владелец", True),
            "not offered": (approvals.callback_data("always", forged), OWNER, CHAT, "Этот выбор недоступен", False),
        }
        for name, (button, user, chat, toast, alert) in cases.items():
            with self.subTest(name):
                reply = await appr.press(button, user, chat)
                self.assertEqual((reply.toast, reply.alert), (toast, alert))
        self.assertEqual(self.hermes.resolved, [])
        self.assertEqual(self.bot.edits, [])
        self.assertTrue(appr.awaiting("c1"))

    async def test_press_while_the_card_is_being_sent_asks_to_wait(self) -> None:
        appr = self.make()
        self.bot.gate = asyncio.get_running_loop().create_future()
        task = asyncio.ensure_future(self.prompt(appr))
        await settle()
        [(_chat, _text, buttons)] = self.bot.sent
        reply = await appr.press(buttons[0][1], OWNER, CHAT)
        self.assertEqual(reply.toast, "Подождите секунду и нажмите снова")
        self.assertEqual(self.hermes.resolved, [])
        self.bot.gate.set_result(None)
        self.assertIsNotNone(await task)

    async def test_request_already_settled_shows_stale_once(self) -> None:
        appr = self.make()
        data = await self.open_card(appr)
        self.hermes.count = 0   # запрос уже решён иначе (таймаут, /approve)
        reply = await appr.press(data["once"], OWNER, CHAT)
        self.assertEqual(reply.toast, "⌛ Запрос неактуален")
        again = await appr.press(data["deny"], OWNER, CHAT)
        self.assertEqual(again.toast, "Запрос неактуален")
        self.assertEqual([text.rsplit("\n\n", 1)[1] for _c, _m, text in self.bot.edits], ["⌛ Запрос неактуален"])

    async def test_stale_press_never_overwrites_a_result_being_settled(self) -> None:
        """Гонка двойного нажатия: «неактуален» поверх «Разрешено» показывать нельзя (ревью S4, п. 10)."""
        appr = self.make()
        data = await self.open_card(appr)
        token = approvals.parse_callback(data["once"])[1]
        self.assertEqual(appr.book.press(token, "once", OWNER, CHAT, chat=CHAT, owner=OWNER)[0], approvals.RESOLVE)
        reply = await appr.press(data["deny"], OWNER, CHAT)
        self.assertEqual(reply.toast, "Запрос неактуален")
        self.assertEqual(self.bot.edits, [])

    async def test_stale_press_on_a_card_still_with_buttons_removes_them(self) -> None:
        appr = self.make()
        card_id = await self.prompt(appr)
        [(_chat, _text, buttons)] = self.bot.sent
        appr.book.expire(card_id)   # например, правка таймаута не прошла — кнопки остались
        await appr.press(buttons[0][1], OWNER, CHAT)
        [(_chat, _mid, text)] = self.bot.edits
        self.assertTrue(text.endswith("\n\n⌛ Запрос неактуален"))
        self.assertEqual(self.hermes.resolved, [])

    async def test_card_from_before_a_restart_is_stale_and_loses_its_buttons(self) -> None:
        """Решение владельца: карточки не сохраняются; нажатие на старую — «неактуален», кнопки снимаются."""
        appr = self.make()
        reply = await appr.press(approvals.callback_data("once", "token-of-old-process"), OWNER, CHAT)
        self.assertEqual((reply.toast, reply.strip), ("Запрос неактуален", True))
        self.assertEqual(self.hermes.resolved, [])


class TurnEndTests(ApprovalsTestCase):
    async def test_hermes_timeout_shows_our_line_instead_of_english(self) -> None:
        """Hermes на таймауте зовёт `edit_message(chat, card_id, «⌛ Approval timed out…»)` адаптера (S4)."""
        appr = self.make()
        card_id = await self.prompt(appr)
        self.assertTrue(await appr.timed_out(card_id))
        [(_chat, message_id, text)] = self.bot.edits
        self.assertEqual(message_id, 42)
        self.assertTrue(text.endswith("\n\n⌛ Время вышло — команда не выполнена."))
        self.assertNotIn("Approval timed out", text)
        self.assertFalse(appr.awaiting("c1"))
        self.assertEqual(self.waiting[-1], ("c1", False))
        self.assertFalse(await appr.timed_out(card_id))
        self.assertFalse(await appr.timed_out("hya-card:unknown"))
        self.assertEqual(len(self.bot.edits), 1)

    async def test_turn_end_marks_live_cards_stale_at_once(self) -> None:
        """Решение владельца 2026-11-17: ход закончился (прерван, отменён) — сразу «неактуален», кнопки сняты."""
        appr = self.make()
        await self.prompt(appr)
        await appr.turn_closed("c1")
        [(_chat, _mid, text)] = self.bot.edits
        self.assertTrue(text.endswith("\n\n⌛ Запрос неактуален"))
        self.assertFalse(appr.awaiting("c1"))
        self.assertEqual(self.waiting[-1], ("c1", False))
        await appr.turn_closed("c1")
        self.assertEqual(len(self.bot.edits), 1)

    async def test_new_turn_clears_the_failure_mark(self) -> None:
        appr = self.make(bot=False)
        await self.prompt(appr)
        self.assertEqual(appr.outcome("c1"), "failed")
        appr.start_turn("c1")
        self.assertEqual(appr.outcome("c1"), "")


class ReviewFixTests(ApprovalsTestCase):
    """Правки по /code-review medium (2026-11-17)."""

    async def test_card_is_edited_and_pressed_in_the_chat_it_was_sent_to(self) -> None:
        """Смена `ALICE_TELEGRAM_CHAT` и переподключение: карточка живёт в чате, куда ушла."""
        appr = self.make(chat="100")
        card_id = await self.prompt(appr)
        [(_chat, _text, buttons)] = self.bot.sent
        appr.configure("200", "100", enabled=True)   # другой чат, владелец прежний
        reply = await appr.press(buttons[0][1], OWNER, 100)
        self.assertEqual(reply.toast, "✅ Разрешено один раз")
        self.assertEqual([chat for chat, _mid, _text in self.bot.edits], [100])
        other = await self.prompt(appr, request_id="r2")
        self.assertTrue(await appr.timed_out(other))
        self.assertEqual([chat for chat, _mid, _text in self.bot.edits], [100, 200])
        self.assertTrue(card_id)

    async def test_failed_edit_is_retried_by_the_next_press(self) -> None:
        """Правка итога не прошла — кнопки остались; следующее нажатие снова пробует их снять."""
        appr = self.make()
        card_id = await self.prompt(appr)
        [(_chat, _text, buttons)] = self.bot.sent
        self.bot.edit_errors.append(RuntimeError("network"))
        self.assertFalse(await appr.timed_out(card_id))
        self.assertEqual(self.bot.edits, [])
        reply = await appr.press(buttons[0][1], OWNER, CHAT)
        self.assertEqual(reply.toast, "Запрос неактуален")
        [(_chat, _mid, text)] = self.bot.edits
        self.assertTrue(text.endswith("\n\n⌛ Запрос неактуален"))

    async def test_turn_end_closes_only_cards_of_that_turn(self) -> None:
        """Конец хода не гасит карточку другого запроса той же беседы."""
        appr = self.make()
        mine = await appr.prompt(alice_chat="c1", session_key="sk", request_id="r1", command=COMMAND,
                                 description=REASON, smart_denied=False, actions=ALL_ACTIONS, turn="turn:m1")
        other = await appr.prompt(alice_chat="c1", session_key="sk", request_id="r2", command=COMMAND,
                                  description=REASON, smart_denied=False, actions=ALL_ACTIONS, turn="turn:m2")
        await appr.turn_closed("c1", "turn:m1")
        self.assertEqual(len(self.bot.edits), 1)
        self.assertTrue(appr.awaiting("c1"))
        self.assertTrue(await appr.timed_out(other))
        self.assertFalse(await appr.timed_out(mine))


class LifetimeTests(ApprovalsTestCase):
    async def test_reconnect_keeps_live_cards(self) -> None:
        """Переподключение адаптера Алисы — новый `configure`, книга та же (ревью S4, п. 10)."""
        appr = self.make()
        await self.prompt(appr)
        [(_chat, _text, buttons)] = self.bot.sent
        appr.configure("100", "", enabled=True, notify=lambda c, w: None)
        reply = await appr.press(buttons[0][1], OWNER, CHAT)
        self.assertEqual(reply.toast, "✅ Разрешено один раз")
        self.assertEqual(self.hermes.resolved, [("sk", "once", "r1")])

    async def test_logs_carry_no_command_and_no_reason(self) -> None:
        logs = []
        appr = self.make()
        await self.prompt(appr)
        [(_chat, _text, buttons)] = self.bot.sent
        await appr.press(buttons[0][1], 777, CHAT)
        await appr.press(buttons[0][1], OWNER, CHAT)
        logs += self.logs
        for kwargs in ({"bot": False}, {"enabled": False}, {"deadline": 0.01}):
            appr = self.make(**kwargs)
            self.bot.gate = asyncio.get_running_loop().create_future()
            await self.prompt(appr)
            self.bot.gate.set_result(None)
            await settle()
            logs += self.logs
        self.assertGreaterEqual(len(logs), 6)
        for line in logs:
            self.assertNotIn("marker-cmd-7f3a", line)
            self.assertNotIn("marker-reason-9c1e", line)


async def settle() -> None:
    """Дать фоновым задачам (поздняя доставка, правки) дойти до конца."""
    for _ in range(10):
        await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
