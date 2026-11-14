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

    def test_missing_marker_or_empty_part_uses_fallback(self):
        for raw in ("Результат без маркеров.", "ГОЛОС:\nТЕЛЕГРАМ: Результат.",
                    "ГОЛОС: Результат.\nТЕЛЕГРАМ:", "ГОЛОС: Результат."):
            with self.subTest(raw=raw):
                voice, telegram, ok = p.split_answer(raw)
                self.assertFalse(ok)
                self.assertEqual(telegram, raw)
                self.assertTrue(voice.endswith(p.VOICE_TAIL))
                self.assertLessEqual(len(voice), 300 + 1 + len(p.VOICE_TAIL))

    def test_fallback_removes_markup_and_links_and_keeps_sentence_boundary(self):
        raw = "**Цена снизилась.** [Источник](https://example.test/item) " + "длинное продолжение " * 40
        voice, _, ok = p.split_answer(raw)
        self.assertFalse(ok)
        self.assertEqual(voice, "Цена снизилась. " + p.VOICE_TAIL)

    def test_speakable_uses_word_boundary_without_sentence(self):
        self.assertEqual(p.speakable("альфа бета гамма дельта", 12), "альфа бета")

    def test_voice_is_clean_and_limited_including_tail(self):
        voice, telegram, ok = p.split_answer("ГОЛОС: " + "**слово** " * 300 + "\nТЕЛЕГРАМ:\nОтвет.")
        self.assertTrue(ok)
        self.assertLessEqual(len(voice), 600)
        self.assertTrue(voice.endswith(p.VOICE_TAIL))
        self.assertNotIn("**", voice)
        self.assertEqual(telegram, "Ответ.")

    def test_alice_reply_hard_limit(self):
        payload = json.loads(p.alice_reply("я" * 2000))
        self.assertEqual(len(payload["response"]["text"]), p.MAX_REPLY)
        self.assertEqual(payload["version"], "1.0")

    def test_marker_lines_inside_body_do_not_break_format(self):
        voice, telegram, ok = p.split_answer("ГОЛОС: Нашла контакт.\nТЕЛЕГРАМ:\nТелефон: 1\nТелеграм: @ivan")
        self.assertEqual((voice, telegram, ok), ("Нашла контакт.", "Телефон: 1\nТелеграм: @ivan", True))


class ReplyTests(unittest.TestCase):
    def test_control_phrases(self):
        for text in ("Голосом!", "подождать", "в Телеграм", "давай голосом", "нет", "да", "не буду"):
            self.assertTrue(p.is_control_phrase(text), text)
        for text in ("", "проверь цену", "пришли прогноз погоды", "скажи курс доллара"):
            self.assertFalse(p.is_control_phrase(text), text)

    def test_intent_priority(self):
        cases = [((), "wait"), (("YANDEX.REJECT",), "telegram"), (("YANDEX.HELP",), "help"),
                 (("YANDEX.REJECT", "bridge.wait"), "wait"), (("YANDEX.CONFIRM", "bridge.no_wait"), "telegram"),
                 (("YANDEX.HELP", "YANDEX.REJECT"), "help"), (("YANDEX.REPEAT",), "wait")]
        for intents, expected in cases:
            nlu = {"intents": {name: {} for name in intents}}
            self.assertEqual(p.classify_reply(nlu), expected, intents)

    def test_malformed_nlu_waits(self):
        for nlu in (None, "x", {}, {"intents": []}, {"intents": {"YANDEX.REJECT": "x"}}):
            self.assertEqual(p.classify_reply(nlu), "wait", nlu)


if __name__ == "__main__":
    unittest.main()
