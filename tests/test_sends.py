"""Что такое отправка шлюза для голосовой платформы — правило итога S1, пометка леджера S5, пересылка S6.

Перенесено из проб `prototypes/s5/probe_s5.py` (RecoveryTests), `prototypes/s6/probe_s6.py` (ForwardTests) и правила
итога из `prototypes/s1/probe_s1.py` (TrackerRuleTests — только правило, без журнала ходов пробы). Hermes не нужен.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "yandex-alice"))
import sends as s  # noqa: E402

RECOVERED_MARKER = ("♻️ Recovered reply — the gateway restarted during delivery, "
                    "so this may be a duplicate:\n\n")
RECONNECTED_MARKER = ("♻️ Recovered reply — the messaging platform reconnected after the original "
                      "delivery failed, so this may be a duplicate:\n\n")
FLOOD_MARKER = ("♻️ Recovered reply — the messaging platform's rate limit refused the original, so part "
                "of it may already have arrived above:\n\n")


def kind(**kw):
    base = dict(open_turn=True, turn_mid="m1", reply_to="m1", notify=True, interim=False, text="Ответ")
    base.update(kw)
    return s.classify_send(**base)


class MarkerTests(unittest.TestCase):
    def test_all_three_hermes_markers_are_cut_by_form(self):
        for marker in (RECOVERED_MARKER, RECONNECTED_MARKER, FLOOD_MARKER):
            with self.subTest(marker=marker[:40]):
                self.assertEqual(s.strip_marker(marker + "Ответ готов."), "Ответ готов.")

    def test_marker_without_variation_selector(self):
        self.assertEqual(s.strip_marker("♻ Any words at all:\n\nОтвет"), "Ответ")

    def test_words_are_never_parsed(self):
        self.assertEqual(s.strip_marker("♻️ Совсем другие слова:\n\nОтвет"), "Ответ")
        self.assertEqual(s.strip_marker("Recovered reply — may be a duplicate:\n\nОтвет"),
                         "Recovered reply — may be a duplicate:\n\nОтвет")

    def test_not_marker_shaped_stays_whole(self):
        for text in ("Ответ", "", "♻️ без двоеточия\n\nОтвет", "♻️ Заголовок:",
                     "♻️ Заголовок:\n\n   ", "Ответ\n\n♻️ Заголовок:\n\nхвост"):
            with self.subTest(text=text):
                self.assertEqual(s.strip_marker(text), text)

    def test_none_is_safe(self):
        self.assertEqual(s.strip_marker(None), "")

    def test_split_marker_returns_the_marker(self):
        self.assertEqual(s.split_marker("♻️ Заголовок:\n\nОтвет"), ("♻️ Заголовок:", "Ответ"))
        self.assertEqual(s.split_marker("Ответ"), ("", "Ответ"))


class ClassifyTests(unittest.TestCase):
    def test_final_is_the_s1_rule(self):
        self.assertEqual(kind(), s.FINAL)
        self.assertEqual(kind(reply_to="m2"), s.OUTSIDE)
        self.assertEqual(kind(notify=None), s.OUTSIDE)
        self.assertEqual(kind(interim=True), s.INTERIM)

    def test_notify_must_be_exactly_true(self):
        for notify in ("yes", 1, None, False):
            with self.subTest(notify=notify):
                self.assertEqual(kind(notify=notify), s.OUTSIDE)

    def test_prefixes_do_not_veto_an_anchored_final(self):
        for prefix in ("⚡", "⏳", "⏩", "💾"):
            with self.subTest(prefix=prefix):
                self.assertEqual(kind(text=f"{prefix} Таймер на 5 минут поставлен."), s.FINAL)

    def test_explicit_interim_marker_is_never_final(self):
        self.assertEqual(kind(text="Сейчас проверю.", interim=True), s.INTERIM)

    def test_home_notice_without_anchor_is_not_final(self):
        self.assertEqual(kind(text="📬 No home channel is set", reply_to=None, notify=None), s.OUTSIDE)

    def test_internal_turn_without_message_id(self):
        self.assertEqual(kind(turn_mid=None, reply_to=None), s.FINAL)
        self.assertEqual(kind(turn_mid=None, reply_to=None, notify=None), s.OUTSIDE)

    def test_send_without_open_turn_is_outside(self):
        for text in ("⏳ later", "late"):
            with self.subTest(text=text):
                self.assertEqual(kind(open_turn=False, turn_mid=None, reply_to=None, notify=None, text=text),
                                 s.OUTSIDE)

    def test_recovered_only_outside_a_turn(self):
        marked = RECOVERED_MARKER + "Ответ"
        self.assertEqual(kind(open_turn=False, turn_mid=None, reply_to=None, notify=None, text=marked), s.RECOVERED)
        # внутри открытого хода текст агента, похожий на пометку, — итог, не режется
        self.assertEqual(kind(text="♻️ Итог:\n\nтекст"), s.FINAL)
        # досылка строки, которая не начинала отправку, — без пометки, неотличима от прочих отправок вне хода
        self.assertEqual(kind(open_turn=False, turn_mid=None, reply_to=None, notify=None), s.OUTSIDE)


class ForwardTests(unittest.TestCase):
    def fwd(self, open_turn, k, text="Sorry, I encountered an error."):
        return s.forward_text(open_turn=open_turn, kind=k, text=text)

    def test_outside_a_turn_goes_to_telegram_with_the_label(self):
        # подпись — как у всего, что плагин шлёт в Telegram (решение 2026-11-17), а не «Алиса:\n\n» пробы S6
        self.assertEqual(self.fwd(False, s.OUTSIDE, "Текст"), "Алиса:\nТекст")

    def test_inside_a_turn_nothing_is_forwarded(self):
        for k in (s.OUTSIDE, s.INTERIM, s.FINAL):
            with self.subTest(kind=k):
                self.assertIsNone(self.fwd(True, k, "📬 No home channel is set"))

    def test_service_recovered_and_final_are_not_forwarded(self):
        for k in (s.INTERIM, s.RECOVERED, s.FINAL):
            with self.subTest(kind=k):
                self.assertIsNone(self.fwd(False, k))

    def test_empty_text_is_not_forwarded(self):
        for text in ("", "  \n", None):
            with self.subTest(text=text):
                self.assertIsNone(self.fwd(False, s.OUTSIDE, text))

    def test_classify_then_forward(self):
        outside = dict(open_turn=False, turn_mid=None, reply_to=None, notify=None, interim=False)
        self.assertEqual(s.classify_send(**outside, text="Sorry, I encountered an error."), s.OUTSIDE)
        marked = "♻️ Recovered reply — may be a duplicate:\n\nОтвет"
        self.assertIsNone(self.fwd(False, s.classify_send(**outside, text=marked), marked))


class ApprovalPromptTests(unittest.TestCase):
    """Текст `/approve` Hermes (`is_approval_prompt`) не озвучивается, не становится незабранным ответом и не уходит в
    Telegram (S4, вывод 5) — в ходе и вне его, с любыми `reply_to`/`notify`."""

    def test_approval_prompt_text_is_never_final_or_forwarded(self):
        for open_turn in (True, False):
            for notify in (True, None):
                with self.subTest(open_turn=open_turn, notify=notify):
                    k = kind(open_turn=open_turn, notify=notify, approval_prompt=True,
                             text="♻️ Approve:\n\n/approve to run")
                    self.assertEqual(k, s.INTERIM)
                    self.assertIsNone(s.forward_text(open_turn=open_turn, kind=k, text="/approve"))


if __name__ == "__main__":
    unittest.main()
