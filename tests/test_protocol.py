"""Тесты чистой логики плагина; Hermes не нужен. Часть перенесена из old-bridge."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "yandex-alice"))
import protocol as p  # noqa: E402

VOICE = "Готово, цена 1500 рублей."
TELEGRAM = "Подробно: цена 1500 рублей, ссылка https://example.test/item"
ANSWER = f"ГОЛОС: {VOICE}\nТЕЛЕГРАМ:\n{TELEGRAM}"


def body(utterance="проверь цену", user_id="owner", skill_id="skill", **session):
    data = {"version": "1.0",
            "session": {"session_id": "s1", "message_id": 3, "skill_id": skill_id, "new": False,
                        "user": {"user_id": user_id}, **session},
            "request": {"original_utterance": utterance, "nlu": {"intents": {}}}}
    return json.dumps(data, ensure_ascii=False).encode()


class ParseTests(unittest.TestCase):
    def test_valid_request(self):
        req = p.parse_request(body("  проверь цену "))
        self.assertEqual((req.text, req.user_id, req.skill_id, req.turn_id), ("проверь цену", "owner", "skill", "s1-3"))

    def test_null_fields_are_absent(self):
        data = json.loads(body())
        data["request"], data["session"]["user"] = None, None
        req = p.parse_request(json.dumps(data).encode())
        self.assertEqual((req.text, req.user_id), ("", ""))

    def test_bad_bodies(self):
        for raw in (b"", b"x" * (p.MAX_BODY + 1), b"{", b"[]", b'{"session": 1}',
                    body(message_id=True), body(new="yes"), body(user_id=5)):
            with self.subTest(raw=raw[:30]):
                with self.assertRaises(p.BadRequest):
                    p.parse_request(raw)

    def test_owner_and_rejected(self):
        self.assertTrue(p.is_owner("a", "a"))
        self.assertFalse(p.is_owner("a", ""))
        self.assertFalse(p.is_owner("a", "b"))
        for text in ("/new", " /stop", "a\x00b", "я" * (p.MAX_TEXT + 1)):
            self.assertTrue(p.is_rejected_command(text))
        self.assertFalse(p.is_rejected_command("проверь цену"))

    def test_rate_limiter(self):
        now = [0.0]
        limiter = p.RateLimiter(2, clock=lambda: now[0])
        self.assertEqual([limiter.allow() for _ in range(3)], [True, True, False])
        now[0] = 61
        self.assertTrue(limiter.allow())


class AnswerTests(unittest.TestCase):
    def test_two_texts_and_tolerant_markers(self):
        for raw in (ANSWER, f"**голос:** {VOICE}\n## Телеграмм:\n{TELEGRAM}",
                    f"# ГОЛОС: {VOICE}\n**ТЕЛЕГРАМ:**\n{TELEGRAM}"):
            with self.subTest(raw=raw):
                self.assertEqual(p.split_answer(raw), (VOICE, TELEGRAM, True))

    def test_unmarked_answer_is_voice_only(self):
        self.assertEqual(p.split_answer("Результат без маркеров."), ("Результат без маркеров.", "", False))

    def test_unmarked_long_answer_is_cleaned_but_not_cut(self):
        raw = "**Цена снизилась.** [Источник](https://example.test/item) " + "длинное продолжение " * 40
        voice, telegram, ok = p.split_answer(raw)
        self.assertEqual((voice, telegram, ok),
                         ("Цена снизилась. Источник " + " ".join(["длинное продолжение"] * 40), "", False))

    def test_marked_long_voice_is_cleaned_but_not_cut(self):
        answer = p.split_answer("ГОЛОС: " + "**слово** " * 300 + "\nТЕЛЕГРАМ:\nОтвет.")
        self.assertEqual(answer, (" ".join(["слово"] * 300), "Ответ.", True))

    def test_voice_marker_only_or_empty_telegram_is_voice_only(self):
        for raw in ("ГОЛОС: Результат.", "ГОЛОС: Результат.\nТЕЛЕГРАМ:"):
            with self.subTest(raw=raw):
                self.assertEqual(p.split_answer(raw), ("Результат.", "", False))

    def test_telegram_without_voice_goes_to_telegram_whole(self):
        cases = [("ТЕЛЕГРАМ:\nСписок дел.", "Список дел."),
                 ("ГОЛОС:\nТЕЛЕГРАМ: Список дел.", "Список дел."),
                 ("Вот что нашла.\nТЕЛЕГРАМ:\nСписок дел.\nГОЛОС: коротко", "Вот что нашла.\nСписок дел.\nГОЛОС: коротко")]
        for raw, telegram in cases:
            with self.subTest(raw=raw):
                self.assertEqual(p.split_answer(raw), ("", telegram, False))

    def test_alice_reply_hard_limit(self):
        payload = json.loads(p.alice_reply("я" * 2000))
        self.assertEqual(len(payload["response"]["text"]), p.MAX_REPLY)
        self.assertEqual(payload["version"], "1.0")

    def test_reply_keeps_session_open_unless_asked_to_close(self):
        self.assertIs(json.loads(p.alice_reply("Готово."))["response"]["end_session"], False)
        self.assertIs(json.loads(p.alice_reply("Нет.", end_session=True))["response"]["end_session"], True)

    def test_marker_lines_inside_body_do_not_break_format(self):
        voice, telegram, ok = p.split_answer("ГОЛОС: Нашла контакт.\nТЕЛЕГРАМ:\nТелефон: 1\nТелеграм: @ivan")
        self.assertEqual((voice, telegram, ok), ("Нашла контакт.", "Телефон: 1\nТелеграм: @ivan", True))


# Тексты итога-ошибки шлюза — по шаблонам Hermes v0.23.1: locales/{en,ru}.yaml (explainer.no_reply_prefix,
# explainer.empty_response) и agent/turn_failure_copy.py (exhausted_copy).
NO_REPLY_EN = ("⚠️ No reply: The model didn't produce a reply this time, even after retries. "
               "Send `continue` to try again, or switch models with /model.")
NO_REPLY_RU = ("⚠️ Нет ответа: Модель в этот раз не дала ответа даже после повторных попыток. "
               "Отправьте `continue`, чтобы попробовать ещё раз, или смените модель через /model.")
EXHAUSTED_TAIL = (" To avoid this in future, add a backup provider with `hermes fallback add`.\n\n"
                  "Provider said: HTTP 500: upstream error")
SERVER_ERROR = ("Custom endpoint returned a server error on all 3 attempts — it looks temporarily unavailable. "
                "Wait a minute and send /retry, or switch models with /model." + EXHAUSTED_TAIL)
RATE_LIMITED = ("Custom endpoint rate-limited every one of 3 attempts — its usage limit resets in 3h. "
                "Send /retry after that, or switch models with /model." + EXHAUSTED_TAIL)


class GatewayErrorTests(unittest.TestCase):
    def test_known_gateway_error_forms(self):
        for text in (NO_REPLY_EN, NO_REPLY_RU, SERVER_ERROR, RATE_LIMITED):
            with self.subTest(text=text[:30]):
                self.assertTrue(p.is_gateway_error(text))

    def test_error_with_leading_whitespace_or_plain_warning_sign(self):
        for text in ("\n" + NO_REPLY_EN, "  " + SERVER_ERROR, NO_REPLY_EN.replace("️", "")):
            with self.subTest(text=text[:30]):
                self.assertTrue(p.is_gateway_error(text))

    def test_ordinary_answers_are_not_errors(self):
        for text in ("⚠️ Внимание: завтра гроза.", "Сайт вернул server error, попробую позже.",
                     "No reply: так и не ответил.", "", "Совет: add a backup provider with `hermes fallback add`."):
            with self.subTest(text=text[:30]):
                self.assertFalse(p.is_gateway_error(text))


class VoiceReplyTests(unittest.TestCase):
    def test_gateway_error_is_spoken_as_failure_and_sent_to_telegram(self):
        self.assertEqual(p.voice_reply(SERVER_ERROR),
                         (["Не получилось, попробуйте ещё раз."], "Алиса:\n" + SERVER_ERROR))

    def test_long_unmarked_answer_is_read_in_parts_without_telegram(self):
        raw = "Пункт номер один готов. " * 80
        parts, telegram = p.voice_reply(raw)
        self.assertGreater(len(parts), 1)
        self.assertEqual(" ".join(parts), raw.strip())
        self.assertEqual(telegram, "")

    def test_unmarked_answer_with_link_is_copied_to_telegram(self):
        raw = "Билеты есть: [касса](https://example.test/tickets)."
        self.assertEqual(p.voice_reply(raw), (["Билеты есть: касса."], "Алиса:\n" + raw))

    def test_marked_answer_sends_details_with_label(self):
        self.assertEqual(p.voice_reply(ANSWER), ([VOICE], "Алиса:\n" + TELEGRAM))

    def test_telegram_only_answer_says_it_will_be_sent(self):
        self.assertEqual(p.voice_reply("ТЕЛЕГРАМ:\nСписок дел."),
                         (["Ответ пришлю в Телеграм."], "Алиса:\nСписок дел."))

    def test_body_lines_like_markers_do_not_change_the_answer(self):
        """Строки «Телеграм: …»/«Голос: …» в обычном тексте — не формат: маркер формата пишется заглавными."""
        cases = [("Контакты Ивана:\nТелеграм: @ivan\nТелефон: 1", ["Контакты Ивана: Телеграм: @ivan Телефон: 1"]),
                 ("Итоги:\nГолос: за — трое.\nПротив — один.", ["Итоги: Голос: за — трое. Против — один."])]
        for raw, parts in cases:
            with self.subTest(raw=raw):
                self.assertEqual(p.voice_reply(raw), (parts, ""))

    def test_text_before_voice_marker_is_kept(self):
        self.assertEqual(p.voice_reply("Итоги.\nГОЛОС: За — трое."), (["Итоги. За — трое."], ""))

    def test_link_copy_has_no_format_markers(self):
        self.assertEqual(p.voice_reply("ГОЛОС: Смотри https://example.test/a"),
                         (["Смотри"], "Алиса:\nСмотри https://example.test/a"))

    def test_links_without_scheme_and_emails_are_copied_to_telegram(self):
        for raw in ("Пиши на t.me/example_user", "Почта: user@example.test"):
            with self.subTest(raw=raw):
                self.assertEqual(p.voice_reply(raw).telegram, "Алиса:\n" + raw)

    def test_empty_answer_is_failure_without_telegram(self):
        for raw in ("", "  \n", "ГОЛОС:\nТЕЛЕГРАМ:"):
            with self.subTest(raw=raw):
                self.assertEqual(p.voice_reply(raw), (["Не получилось, попробуйте ещё раз."], ""))


class VoicePartsTests(unittest.TestCase):
    def test_short_text_is_one_part_and_empty_text_has_none(self):
        self.assertEqual(p.voice_parts("Готово, цена 1500 рублей."), ["Готово, цена 1500 рублей."])
        self.assertEqual(p.voice_parts(""), [])

    def test_long_text_is_cut_at_sentence_boundary_within_limit(self):
        first = "Первое предложение. " + "а" * 879 + "."   # 900 символов ровно
        self.assertEqual(len(first), 900)
        self.assertEqual(p.voice_parts(first), [first])
        self.assertEqual(p.voice_parts(first + " Ещё."), [first, "Ещё."])

    def test_sentence_over_limit_is_cut_by_words(self):
        self.assertEqual(p.voice_parts("альфа бета гамма дельта", 12), ["альфа бета", "гамма дельта"])

    def test_word_over_limit_is_cut_hard(self):
        self.assertEqual(p.voice_parts("да абвгдежзик нет", 5), ["да", "абвгд", "ежзик", "нет"])

    def test_parts_keep_all_text_and_fit_limit(self):
        texts = ["Коротко.", "слово " * 400, "Раз. " * 300, "x" * 2500, "Начало. " + "y" * 950 + " конец. Хвост!",
                 "Вопрос? Ответ… Восклицание! " * 60]
        for text in texts:
            text = " ".join(text.split())
            with self.subTest(text=text[:20]):
                parts = p.voice_parts(text)
                self.assertTrue(all(0 < len(part) <= p.VOICE_LIMIT and part == part.strip() for part in parts))
                self.assertEqual("".join(parts).replace(" ", ""), text.replace(" ", ""))
                if "x" * 950 not in text and "y" * 950 not in text:
                    self.assertEqual(" ".join(parts), text)


class TextTests(unittest.TestCase):
    # Формулировки — дословно из docs/RECON.md («Модель беседы», М1–М6, открытые вопросы 10–11).
    VERBATIM = {
        "REPLY_ACCEPTED": "Вопрос принят. Спросите ответ позже или готовы подождать?",
        "REPLY_LATER": "Хорошо, спросите ответ позже.",
        "REPLY_NOT_READY": "Ещё не готово. Подождёте?",
        "REPLY_NEW_OR_OLD": "Начать новый диалог или продолжить прошлый?",
        "REPLY_READY": "Ответ на прошлый вопрос готов. Скинуть?",
        "REPLY_NO_ANSWERS": "Ответов нет, задайте вопрос.",
        "REPLY_FAILED": "Не получилось, попробуйте ещё раз.",
        "REPLY_LOST": "Вопрос потерялся, повторите.",
        "REPLY_CARD_FAILED": "Запрос на подтверждение не дошёл до Телеграма, команда не выполнена.",
        "REPLY_CONTINUE": "Продолжить?",
    }

    def test_dialog_texts_are_verbatim(self):
        for name, text in self.VERBATIM.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(p, name, None), text)

    def test_all_texts_are_speakable_in_one_reply(self):
        texts = {name: value for name, value in vars(p).items() if name.startswith("REPLY_")}
        self.assertGreaterEqual(len(texts), 18)
        for name, text in texts.items():
            with self.subTest(name=name):
                self.assertLessEqual(len(text), p.VOICE_LIMIT)
                self.assertEqual(p.voice_parts(text), [text])
                self.assertNotRegex(text, r"[*_`#\[\]<>]|https?://")


class DialogTextTests(unittest.TestCase):
    """Тексты среза «б» — дословно из решений владельца 2026-11-17 (docs/PLAN.md, этап 2)."""
    VERBATIM = {
        "REPLY_SAY_NEW_OR_OLD": "Скажите „новый“ или „прошлый“.",
        "REPLY_ANSWER_WAITS": "Хорошо, ответ подождёт.",
        "REPLY_REST_IN_TELEGRAM": "Хорошо, остальное пришлю в Телеграм.",
        "REPLY_OK": "Хорошо.",
        "REPLY_HELLO_CONTINUE": "Слушаю. Продолжаем прошлый разговор.",
        "REPLY_STILL_WORKING": "Всё ещё работаю. Подождёте?",
        "REPLY_NOT_YET": "Пока нет. Ждём дальше?",
        "REPLY_WAIT_APPROVAL": "Жду подтверждения в Телеграме. Подождёте?",
        "REPLY_TG_FAILED": "Не смогла отправить в Телеграм. Озвучить?",
        "REPLY_TG_UNCERTAIN": "Возможно, ответ не дошёл до Телеграма.",
        "REPLY_HELP": "Задайте вопрос, я передам его агенту. Пока ответ готовится, можно подождать, спросить позже или "
                      "сказать „пришли в Телеграм“. „Новый диалог“ начинает разговор заново.",
        "REPLY_NOTHING_TO_REPEAT": "Пока нечего повторить.",
        "REPLY_PING": "pong",
    }

    def test_texts_are_verbatim(self):
        for name, text in self.VERBATIM.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(p, name, None), text)

    def test_wait_rounds_order(self):
        self.assertEqual(p.WAIT_ROUNDS, ("Ещё не готово. Подождёте?", "Всё ещё работаю. Подождёте?",
                                         "Пока нет. Ждём дальше?"))

    def test_whole_text_strips_markers_and_keeps_body(self):
        self.assertEqual(p.whole_text("ГОЛОС: Цена 1500.\nТЕЛЕГРАМ: Счёт №7.\nТелеграм: @ivan"),
                         "Алиса:\nЦена 1500.\nСчёт №7.\nТелеграм: @ivan")

    def test_whole_text_of_gateway_error_is_as_is(self):
        raw = "⚠️ No reply: the model didn't produce a reply."
        self.assertEqual(p.whole_text(f"  {raw}\n"), "Алиса:\n" + raw)


class IntentTests(unittest.TestCase):
    def test_own_and_builtin_intents_are_recognized(self):
        names = ["hya.answer.get", "hya.dialog.new", "hya.dialog.continue", "hya.wait", "hya.later",
                 "hya.telegram", "hya.next", "YANDEX.CONFIRM", "YANDEX.REJECT", "YANDEX.HELP", "YANDEX.REPEAT"]
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(p.intents({"intents": {name: {"slots": {}}}}), ("valid", (name,)))

    def test_unknown_names_and_slots_are_dropped(self):
        nlu = {"intents": {"bridge.wait": {}, "hya.wait": {"slots": {"x": {"value": "секрет"}}}, "YANDEX.REJECT": "x"}}
        self.assertEqual(p.intents(nlu), ("valid", ("hya.wait",)))

    def test_malformed_or_absent_nlu(self):
        for nlu, status in ((None, "absent"), ({}, "absent"), ("x", "invalid"), ({"intents": []}, "invalid")):
            with self.subTest(nlu=nlu):
                self.assertEqual(p.intents(nlu), (status, ()))
        self.assertEqual(p.intents({"intents": {}}), ("valid", ()))


if __name__ == "__main__":
    unittest.main()
