"""Подмена адреса cron из голоса — перенесено из пробы S6 (`prototypes/s6/probe_s6.py`, GuardTests). Hermes не нужен."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "yandex-alice"))
import cron_guard as g  # noqa: E402

CHAT = "111"  # синтетический чат Telegram
TARGET = "telegram:" + CHAT


class GuardTests(unittest.TestCase):
    def guard(self, **args):
        return g.guard_args(args, CHAT)

    def test_create_without_deliver_goes_to_telegram(self) -> None:
        self.assertEqual(self.guard(action="create", schedule="in 1m", prompt="p"), {"deliver": TARGET})

    def test_alice_addresses_are_replaced(self) -> None:
        for value in ("origin", " Origin ", "yandex_alice", "yandex_alice:chat-1", "YANDEX_ALICE:x"):
            self.assertEqual(self.guard(action="create", deliver=value), {"deliver": TARGET}, value)

    def test_explicit_choice_stays(self) -> None:
        for value in ("local", "telegram:222", TARGET, "all", "bot-chat:p2", "slack:#x"):
            self.assertIsNone(self.guard(action="create", deliver=value), value)

    def test_lists_replace_only_alice_tokens(self) -> None:
        self.assertEqual(self.guard(action="create", deliver="origin,local"), {"deliver": TARGET + ",local"})
        self.assertEqual(self.guard(action="create", deliver=["local", "origin"]), {"deliver": "local," + TARGET})
        self.assertEqual(self.guard(action="create", deliver="origin, yandex_alice:x," + TARGET), {"deliver": TARGET})

    def test_update_without_deliver_keeps_the_old_target(self) -> None:
        # При update не указанный deliver значит «не менять» (задача из Telegram с deliver=local остаётся).
        self.assertIsNone(self.guard(action="update", job_id="j1", schedule="every 2h"))
        self.assertIsNone(self.guard(action="update", job_id="j1", deliver=""))
        self.assertEqual(self.guard(action="update", job_id="j1", deliver="origin"), {"deliver": TARGET})

    def test_failure_deliver_only_explicit_alice(self) -> None:
        self.assertEqual(self.guard(action="create", deliver="local", failure_deliver="origin"),
                         {"failure_deliver": TARGET})
        self.assertEqual(self.guard(action="create", failure_deliver="yandex_alice"),
                         {"deliver": TARGET, "failure_deliver": TARGET})
        self.assertEqual(self.guard(action="create", deliver="local", failure_deliver=""), None)
        self.assertEqual(self.guard(action="update", job_id="j1", failure_deliver="origin"),
                         {"failure_deliver": TARGET})

    def test_other_actions_untouched(self) -> None:
        for action in ("list", "run", "pause", "resume", "remove", "cancel", None, 5):
            self.assertIsNone(self.guard(action=action, deliver="origin", job_id="j1"), action)

    def test_no_chat_no_rewrite(self) -> None:
        for chat in ("", "  ", None, "1,2", "1 2", "a:b"):
            self.assertIsNone(g.guard_args({"action": "create", "deliver": "origin"}, chat), chat)

    def test_garbage_never_raises(self) -> None:
        # Бросающий хук pre_tool_call заблокировал бы все инструменты профиля: на мусоре — None, не ошибка.
        for args in (None, [], "create", 5, {"action": ["create"]}, {"action": "create", "deliver": 5},
                     {"action": "create", "deliver": ["origin", 5]}, {"action": "update", "failure_deliver": {}}):
            self.assertIsNone(g.guard_args(args, CHAT), args)

    def test_action_is_normalized_like_hermes(self) -> None:
        # tools/cronjob_tools.py: (action or "").strip().lower()
        self.assertEqual(self.guard(action=" CREATE ", prompt="p"), {"deliver": TARGET})
        self.assertEqual(self.guard(action="Update", job_id="j1", deliver="origin"), {"deliver": TARGET})


class ValidChatTests(unittest.TestCase):
    def test_bad_values_are_rejected(self) -> None:
        # Для validate_config адаптера: такое значение нельзя подставить в telegram:<чат>.
        for chat in (None, 5, "", "   ", "1,2", "1 2", "a:b", "-100:7"):
            self.assertIsNone(g.valid_chat(chat), repr(chat))

    def test_good_value_is_stripped(self) -> None:
        self.assertEqual(g.valid_chat("111"), "111")
        self.assertEqual(g.valid_chat(" -100123 "), "-100123")


if __name__ == "__main__":
    unittest.main()
