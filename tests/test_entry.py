"""Порядок проверок HTTP-входа (docs/DESIGN.md, этап 3, срез «б», б1) — без Hermes и без aiohttp.

Секрет, skill и user здесь выдуманные.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "yandex-alice"))
import entry as e  # noqa: E402
import protocol as p  # noqa: E402

SECRET = "s3cret-path-for-tests"
CFG = e.EntryConfig(secret=SECRET, skill_id="skill-1", owner_user_id="u-owner")


class PathTests(unittest.TestCase):
    def test_wrong_path_is_refused(self) -> None:
        self.assertFalse(e.check_path("not-the-secret", CFG))
        self.assertTrue(e.check_path(SECRET, CFG))

    def test_empty_secret_refuses_everything(self) -> None:
        # Платформа без секрета не должна принимать даже пустой путь.
        nosecret = e.EntryConfig(secret="", skill_id="skill-1", owner_user_id="u-owner")
        for tail in ("", "x"):
            self.assertFalse(e.check_path(tail, nosecret), repr(tail))


def body(text="привет", skill="skill-1", user="u-owner", new=False, **extra) -> bytes:
    session = {"session_id": "sess-1", "message_id": 1, "skill_id": skill, "new": new}
    if user is not None:
        session["user"] = {"user_id": user}
    request = {"original_utterance": text}
    request.update(extra)
    return json.dumps({"session": session, "request": request}, ensure_ascii=False).encode()


class ScreenTests(unittest.TestCase):
    def test_oversized_body_is_413(self) -> None:
        self.assertEqual(e.screen(b"x" * (p.MAX_BODY + 1), CFG), e.Reject(413))

    def test_bad_format_is_400(self) -> None:
        for raw in (b"", b"not json", b"[]", b'{"session": 5}', body().replace(b'"new": false', b'"new": 5')):
            self.assertEqual(e.screen(raw, CFG), e.Reject(400), raw)

    def test_ping_is_answered_before_skill_and_user(self) -> None:
        # Яндекс проверяет живость навыка: в ping может не быть session.user, а skill_id — чужим.
        for text in ("ping", " PING "):
            out = e.screen(body(text, skill="other-skill", user=None), CFG)
            self.assertEqual(out, e.Reply(p.alice_reply(p.REPLY_PING)), text)

    def test_foreign_skill_is_refused_and_the_session_closed(self) -> None:
        out = e.screen(body(skill="other-skill"), CFG)
        self.assertEqual(out, e.Reply(p.alice_reply(p.REPLY_NOT_OWNER, end_session=True)))

    def test_foreign_or_anonymous_user_is_refused_even_with_empty_text(self) -> None:
        refusal = e.Reply(p.alice_reply(p.REPLY_NOT_OWNER, end_session=True))
        for user in ("u-stranger", None, ""):
            for text in ("привет", ""):
                self.assertEqual(e.screen(body(text, user=user), CFG), refusal, (user, text))

    def test_owner_passes(self) -> None:
        out = e.screen(body("привет", new=True), CFG)
        self.assertIsInstance(out, e.Passed)
        self.assertEqual((out.request.text, out.request.user_id, out.request.new), ("привет", "u-owner", True))

    def test_binding_mode_refuses_everyone_and_reports_the_user_id(self) -> None:
        unbound = e.EntryConfig(secret=SECRET, skill_id="skill-1", owner_user_id="")
        seen = []
        for user in ("u-owner", "u-stranger", None):
            out = e.screen(body(user=user), unbound, seen.append)
            self.assertEqual(out, e.Reply(p.alice_reply(p.REPLY_UNBOUND, end_session=True)), user)
        self.assertEqual(seen, ["u-owner", "u-stranger", ""])

    def test_unbound_report_goes_to_the_log_once_per_run(self) -> None:
        lines = []
        note = e.OnceNote(lines.append)
        for _ in range(3):
            note("u-owner")
        self.assertEqual(len(lines), 1)
        self.assertIn("ALICE_OWNER_USER_ID", lines[0])
        self.assertIn("u-owner", lines[0])


class OrderTests(unittest.TestCase):
    """Порядок: путь → размер → формат → ping → skill → привязка/владелец (PLAN, этап 3)."""

    def test_ping_beats_skill_and_binding(self) -> None:
        unbound = e.EntryConfig(secret=SECRET, skill_id="skill-1", owner_user_id="")
        seen = []
        out = e.screen(body("ping", skill="other-skill", user="u-stranger"), unbound, seen.append)
        self.assertEqual(out, e.Reply(p.alice_reply(p.REPLY_PING)))
        self.assertEqual(seen, [])

    def test_skill_beats_binding(self) -> None:
        unbound = e.EntryConfig(secret=SECRET, skill_id="skill-1", owner_user_id="")
        seen = []
        out = e.screen(body(skill="other-skill"), unbound, seen.append)
        self.assertEqual(out, e.Reply(p.alice_reply(p.REPLY_NOT_OWNER, end_session=True)))
        self.assertEqual(seen, [])   # user_id чужого навыка в лог не попадает


if __name__ == "__main__":
    unittest.main()
