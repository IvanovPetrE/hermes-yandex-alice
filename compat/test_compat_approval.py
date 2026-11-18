"""Совместимость: перезапуск шлюза посреди ожидания подтверждения команды (S5 R4, этап 6, срез «в»).

Telegram — `TelegramStandin` из тестов Hermes (поддельный Bot API в процессе теста, шлюз-ребёнок ходит к нему по `base_url`):
карточка подтверждения рисуется настоящим Telegram-адаптером Hermes и нашим кодом. Запуск — `run.sh <checkout> <python> T`.
"""
import asyncio
import unittest

import harness
from restart import RestartWorld

OWNER_TG, CHAT = 5001, "5001"   # синтетические: владелец и его личный чат
STALE_TOAST = "Запрос неактуален"
WAIT_APPROVAL = "Жду подтверждения в Телеграме. Подождёте?"


def _fakes():
    from tests.fakes import fake_llm_provider as f
    return f


async def poll(pred, timeout: float, step: float = 0.5):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        got = pred()
        if got:
            return got
        await asyncio.sleep(step)
    return None


def cards(standin) -> list:
    return [b for b in standin.buttons(CHAT) if str(b.get("callback_data", "")).startswith("hya:")]


async def sc_approval_restart(w) -> None:
    """SIGKILL, пока шлюз ждёт нажатия карточки: команда не выполнена, старая карточка мертва, повтор команды рисует новую."""
    f = _fakes()
    standin = w.state["standin"]
    victim = w.home / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("compat", encoding="utf-8")
    command = f"rm -r {victim}"
    w.script.add("t01", f.ToolCall("terminal", {"command": command}))
    w.script.resume_response = f.ToolCall("terminal", {"command": command})   # агент повторяет команду после рестарта
    first = await w.start()
    w.state["polling"] = await poll(lambda: standin.calls_of("getUpdates"), 60.0)
    w.voice["q"] = [await w.say("удали [t01] папку")]
    old = await poll(lambda: cards(standin), 90.0)
    w.state["old_cards"] = list(old or [])
    w.state["wait_before"] = await w.say("да", confirm=True)
    await w.kill(first)
    w.state["victim_after_kill"] = victim.exists()
    second = await w.start()
    old_data = {b["callback_data"] for b in w.state["old_cards"]}
    fresh = await poll(lambda: [b for b in cards(standin) if b["callback_data"] not in old_data], 120.0)
    w.state["new_cards"] = list(fresh or [])
    w.state["wait_after"] = await w.say("да", confirm=True)
    # нажатие карточки прошлого процесса: «Запрос неактуален», команда не выполняется
    button = w.state["old_cards"][0]
    update = standin.callback(OWNER_TG, int(CHAT), int(button["message_id"]), button["callback_data"])
    qid = update["callback_query"]["id"]
    answer = await poll(lambda: [c for c in standin.calls_of("answerCallbackQuery")
                                 if str(c.params.get("callback_query_id")) == qid], 30.0)
    w.state["stale_answer"] = answer[0].params.get("text") if answer else None
    await asyncio.sleep(2)
    w.state["victim_end"] = victim.exists()
    w.state["resume_notes"] = list(w.script.resume_notes)
    w.state["second_alive"] = second.alive()


class GroupT(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tests.fakes.platforms.telegram_standin import TelegramStandin

        standin = TelegramStandin()
        standin.start()
        try:
            cls.world = RestartWorld(
                "approval",
                platform_lines="    gateway_restart_notification: false\n",
                config_extra=("  telegram:\n    enabled: true\n    extra:\n"
                              f"      base_url: {standin.api_base}\n      base_file_url: {standin.file_base}\n"
                              "approvals:\n  mode: manual\n  timeout: 300\n"
                              "platform_toolsets:\n  yandex_alice: [terminal]\n"),
                env_extra=(f"TELEGRAM_BOT_TOKEN={standin.token}\nTELEGRAM_ALLOWED_USERS={OWNER_TG}\n"
                           "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS=1\nHERMES_TELEGRAM_TEXT_BATCH_DELAY_SECONDS=0\n"
                           f"ALICE_TELEGRAM_CHAT={CHAT}\nALICE_TELEGRAM_OWNER_ID={OWNER_TG}\n"))
            cls.world.state["standin"] = standin
            cls.world.run([("approval", sc_approval_restart)])
        finally:
            print(f"\n=== errors={cls.world.errors} voice={cls.world.voice} state="
                  f"{ {k: v for k, v in cls.world.state.items() if k != 'standin'} }")
            print(standin.describe())
            standin.stop()

    def scenario_ok(self) -> None:
        self.assertNotIn("approval", self.world.errors, self.world.errors.get("approval"))

    def test_card_is_drawn_and_voice_waits_for_it(self) -> None:
        self.scenario_ok()
        st = self.world.state
        self.assertTrue(st["old_cards"], "карточка не нарисована: сцена ничего не доказывает")
        self.assertEqual(st["wait_before"], WAIT_APPROVAL)

    def test_sigkill_while_waiting_leaves_the_command_unrun(self) -> None:
        self.scenario_ok()
        st = self.world.state
        self.assertTrue(st["victim_after_kill"], "команда выполнилась без нажатия")
        self.assertTrue(st["victim_end"], "команда выполнилась после перезапуска без нажатия")

    def test_resumed_turn_draws_a_new_card(self) -> None:
        self.scenario_ok()
        st = self.world.state
        self.assertTrue(st["resume_notes"], "автопродолжения не было")
        self.assertTrue(st["new_cards"], "после перезапуска не нарисована новая карточка")
        self.assertEqual(st["wait_after"], WAIT_APPROVAL)

    def test_old_card_is_stale_after_restart(self) -> None:
        self.scenario_ok()
        self.assertEqual(self.world.state["stale_answer"], STALE_TOAST)


if __name__ == "__main__":
    unittest.main()
