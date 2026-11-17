"""Правило «последнего ответа» из истории Hermes — перенесено из пробы S7 (`prototypes/s7/probe_s7.py`, HistoryTests,
только чистая часть). Тесты `read_last_answer` (SessionDB, блокировка) — в срезе «б». Hermes не нужен."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "yandex-alice"))
import history as h  # noqa: E402

PLATFORM = "yandex_alice"


class FakeDb:
    """Только `get_messages(latest=True, limit, offset)` над списком, старые первыми."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def get_messages(self, session_id, latest=False, limit=None, offset=0, **kw):
        self.calls += 1
        assert latest
        newest_first = self.rows[::-1][offset:][:limit]
        return newest_first[::-1]


class Route:
    def __init__(self, platform, profile, enabled=True, **kw):
        self.platform, self.profile, self.enabled = platform, profile, enabled
        for name in ("chat_id", "thread_id", "guild_id", "user_id"):
            setattr(self, name, kw.get(name))


def asst(text=None, tools=False, summary=False):
    row = dict(role="assistant", content=text, active=1)
    if tools:
        row["tool_calls"] = [{"id": "c1"}]
    if summary:
        row["_compressed_summary"] = True
    return row


class HistoryTests(unittest.TestCase):
    def test_newest_assistant_text_wins(self) -> None:
        rows = [asst("старый"), dict(role="user", content="вопрос"), asst("новый")]
        self.assertEqual(h.last_answer(rows), "новый")

    def test_comment_next_to_a_tool_call_is_not_the_answer(self) -> None:
        rows = [asst("Итог."), dict(role="user", content="q"), asst("Сейчас запишу.", tools=True),
                dict(role="tool", content="ok")]
        self.assertEqual(h.last_answer(rows), "Итог.")

    def test_summary_empty_and_non_text_are_skipped(self) -> None:
        rows = [asst("Итог."), asst("Сводка сжатия", summary=True), asst(""), asst("   "), asst(None),
                dict(role="assistant", content=[{"type": "text", "text": "части"}])]
        self.assertEqual(h.last_answer(rows), "Итог.")

    def test_user_and_tool_rows_are_never_answers(self) -> None:
        self.assertIsNone(h.last_answer([dict(role="user", content="q"), dict(role="tool", content="r")]))
        self.assertIsNone(h.last_answer([]))
        self.assertIsNone(h.last_answer(None))

    def test_tail_is_read_in_portions_until_an_answer(self) -> None:
        rows = [asst("Давний ответ")] + [asst(None, tools=True), dict(role="tool", content="r")] * 3
        db = FakeDb(rows)
        self.assertEqual(h.read_tail(db, "s", tail=2, portions=5), ("Давний ответ", 4))

    def test_portions_are_capped(self) -> None:
        rows = [asst("Давний ответ")] + [asst(None, tools=True), dict(role="tool", content="r")] * 6
        db = FakeDb(rows)
        self.assertEqual(h.read_tail(db, "s", tail=2, portions=3), (None, 3))
        self.assertEqual(db.calls, 3)

    def test_short_history_stops_early(self) -> None:
        db = FakeDb([dict(role="user", content="q")])
        self.assertEqual(h.read_tail(db, "s", tail=20, portions=5), (None, 1))

    def test_target_profile_comes_from_the_plain_route(self) -> None:
        routes = [Route("telegram", "tg"), Route(PLATFORM, "off", enabled=False), Route(PLATFORM, "narrow", user_id="u"),
                  Route(PLATFORM, "target")]
        self.assertEqual(h.pick_profile(routes, PLATFORM), "target")
        self.assertEqual(h.pick_profile([], PLATFORM), "default")
        self.assertEqual(h.pick_profile(None, PLATFORM), "default")


if __name__ == "__main__":
    unittest.main()
