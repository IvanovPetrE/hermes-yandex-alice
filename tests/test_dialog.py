"""Машина беседы (docs/RECON.md, «Модель беседы»; docs/PLAN.md, этап 2, срез «б») — без Hermes и без времени.

Тексты для владельца сверяются литералами, а не константами protocol (решения владельца — источник ожидаемого).
"""
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "yandex-alice"))
import dialog as d  # noqa: E402

CONV, FRESH = "conv-1", "conv-2"


def utter(text, *intents, now=100.0, turn="s1-1", launch=False):
    return d.Utterance(now=now, turn_id=turn, text=text, intents=tuple(intents), launch=launch, fresh_id=FRESH)


class AskTests(unittest.TestCase):
    def test_phrase_in_plain_dialog_goes_to_agent_and_answer_is_voiced(self):
        st, acts = d.step(d.State(conversation=CONV), utter("какая погода"))
        self.assertEqual(acts, (d.Ask(CONV, "какая погода"), d.Hold()))
        st, acts = d.step(st, d.Final(now=101.0, conversation=CONV, text="Солнечно."))
        self.assertEqual(acts, (d.Say("Солнечно."),))

    def test_budget_over_after_ask_offers_to_wait(self):
        st, _ = d.step(d.State(conversation=CONV), utter("какая погода"))
        st, acts = d.step(st, d.Expired(now=103.5))
        self.assertEqual(acts, (d.Say("Вопрос принят. Спросите ответ позже или готовы подождать?"),))
        self.assertEqual(st.prompt, d.WAIT)


def waiting():
    """Вопрос в работе, Алиса сказала «Вопрос принят… подождать?»."""
    st, _ = d.step(d.State(conversation=CONV), utter("какая погода", turn="s1-0", now=90.0))
    st, _ = d.step(st, d.Expired(now=93.5))
    return st


YES, NO, HELP, REPEAT = "YANDEX.CONFIRM", "YANDEX.REJECT", "YANDEX.HELP", "YANDEX.REPEAT"


class WaitLoopTests(unittest.TestCase):
    def test_wait_loop_rotates_three_texts_without_limit(self):
        st = waiting()
        heard = []
        for i in range(4):
            st, acts = d.step(st, utter("да", YES, turn=f"s1-{i + 1}"))
            self.assertEqual(acts, (d.Hold(),))
            st, acts = d.step(st, d.Expired(now=103.5 + i))
            heard.append(acts[0].text)
        self.assertEqual(heard, ["Ещё не готово. Подождёте?", "Всё ещё работаю. Подождёте?", "Пока нет. Ждём дальше?",
                                 "Ещё не готово. Подождёте?"])

    def test_waiting_for_telegram_approval_is_said(self):
        st, _ = d.step(waiting(), d.Approval(now=95.0, conversation=CONV, waiting=True))
        st, _ = d.step(st, utter("подожду", "hya.wait"))
        st, acts = d.step(st, d.Expired(now=103.5))
        self.assertEqual(acts, (d.Say("Жду подтверждения в Телеграме. Подождёте?"),))

    def test_no_or_later_on_wait_means_ask_later_by_voice(self):
        for intent, text in ((NO, "нет"), ("hya.later", "позже")):
            with self.subTest(intent=intent):
                st, acts = d.step(waiting(), utter(text, intent))
                self.assertEqual(acts, (d.Say("Хорошо, спросите ответ позже."),))
                self.assertIsNotNone(st.question)   # агент работает дальше (М4)

    def test_answer_arriving_unattended_is_kept_and_yes_voices_it(self):
        st, acts = d.step(waiting(), d.Final(now=95.0, conversation=CONV, text="Солнечно."))
        self.assertEqual(acts, ())
        st, acts = d.step(st, utter("да", YES))
        self.assertEqual(acts, (d.Say("Солнечно."),))

    def test_no_on_wait_with_ready_answer_keeps_it_unclaimed(self):
        st, _ = d.step(waiting(), d.Final(now=95.0, conversation=CONV, text="Солнечно."))
        st, acts = d.step(st, utter("нет", NO))
        self.assertEqual(acts, (d.Say("Хорошо, ответ подождёт."),))
        st, acts = d.step(st, utter("скинь ответ", "hya.answer.get", turn="s1-2"))
        self.assertEqual(acts, (d.Say("Солнечно."),))


def later():
    """Вопрос в работе, Алиса сказала «Хорошо, спросите ответ позже.»."""
    st, _ = d.step(waiting(), utter("нет", NO, turn="s1-00", now=94.0))
    return st


def ready():
    """Незабранный ответ «Солнечно.», Алиса ничего не спрашивает."""
    st, _ = d.step(later(), d.Final(now=95.0, conversation=CONV, text="Солнечно."))
    return st


def forked(base):
    st, acts = d.step(base, utter("а завтра?", turn="s1-01", now=96.0))
    assert acts == (d.Say("Начать новый диалог или продолжить прошлый?"),), acts
    return st


class ForkTests(unittest.TestCase):
    def test_other_phrase_while_answer_pending_asks_new_or_old(self):
        for name, base in (("вопрос в работе", later), ("ответ готов", ready)):
            with self.subTest(name):
                st, acts = d.step(base(), utter("а завтра?"))
                self.assertEqual(acts, (d.Say("Начать новый диалог или продолжить прошлый?"),))
                self.assertEqual(st.prompt, d.NEW_OR_OLD)

    def test_new_makes_the_held_phrase_the_first_question(self):
        st, acts = d.step(forked(later()), utter("новый", "hya.dialog.new"))
        self.assertEqual(acts, (d.Ask(FRESH, "а завтра?"), d.Hold()))
        self.assertEqual((st.conversation, st.orphans), (FRESH, (CONV,)))

    def test_new_sends_the_ready_answer_to_telegram(self):
        st, acts = d.step(forked(ready()), utter("новый", "hya.dialog.new"))
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\nСолнечно.", ("Солнечно.",)), d.Ask(FRESH, "а завтра?"),
                                d.Hold()))

    def test_old_voices_the_answer_or_waits_and_drops_the_phrase(self):
        st, acts = d.step(forked(ready()), utter("прошлый", "hya.dialog.continue"))
        self.assertEqual(acts, (d.Say("Солнечно."),))
        st, acts = d.step(forked(later()), utter("прошлый", "hya.dialog.continue"))
        self.assertEqual(acts, (d.Hold(),))
        self.assertEqual(st.held, "")

    def test_yes_or_no_on_the_fork_is_asked_again(self):
        for intent in (YES, NO):
            with self.subTest(intent=intent):
                st, acts = d.step(forked(ready()), utter("да", intent))
                self.assertEqual(acts, (d.Say("Скажите „новый“ или „прошлый“."),))
                self.assertEqual(st.held, "а завтра?")

    def test_orphan_final_goes_to_telegram_and_leaves_the_new_dialog_alone(self):
        st, _ = d.step(forked(later()), utter("новый", "hya.dialog.new"))
        st2, acts = d.step(st, d.Final(now=97.0, conversation=CONV, text="Солнечно."))
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\nСолнечно."),))
        self.assertEqual((st2.question, st2.holding, st2.orphans), (st.question, st.holding, ()))
        st2, acts = d.step(st2, d.Final(now=98.0, conversation=FRESH, text="Дождь."))
        self.assertEqual(acts, (d.Say("Ответ пришлю в Телеграм. Дождь."),))

    def test_orphan_turn_closed_without_final_reports_failure_in_telegram(self):
        st, _ = d.step(forked(later()), utter("новый", "hya.dialog.new"))
        st2, acts = d.step(st, d.TurnClosed(now=97.0, conversation=CONV))
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\nНе получилось, попробуйте ещё раз."),))
        self.assertEqual((st2.question, st2.orphans), (st.question, ()))

    def test_send_to_telegram_ready_answer_goes_now(self):
        st, acts = d.step(ready(), utter("пришли в телеграм", "hya.telegram"))
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\nСолнечно.", ("Солнечно.",)), d.Say("Ответ пришлю в Телеграм.")))
        self.assertIsNone(st.answer)

    def test_send_to_telegram_while_in_work_sends_the_final_there(self):
        for base in (waiting, later):
            with self.subTest(base.__name__):
                st, acts = d.step(base(), utter("пришли в телеграм", "hya.telegram"))
                self.assertEqual(acts, (d.Say("Ответ пришлю в Телеграм."),))
                st, acts = d.step(st, d.Final(now=99.0, conversation=CONV, text="Солнечно."))
                self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\nСолнечно.", ("Солнечно.",)),))
                self.assertIsNone(st.answer)

    def test_internal_turn_without_question_goes_to_telegram_only(self):
        st, acts = d.step(answered(), d.Final(now=150.0, conversation=CONV, text="Фоновая задача готова.", internal=True))
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\nФоновая задача готова."),))
        self.assertIsNone(st.answer)

    def test_internal_turn_with_question_in_work_is_the_unclaimed_answer(self):
        st, acts = d.step(later(), d.Final(now=150.0, conversation=CONV, text="Солнечно.", internal=True))
        self.assertEqual(acts, ())
        _, acts = d.step(st, launch(now=160.0))
        self.assertEqual(acts, (d.Say("Ответ на прошлый вопрос готов. Скинуть?"),))

    def test_promised_telegram_answer_is_voiced_too_if_waited_for(self):
        st, _ = d.step(later(), utter("пришли в телеграм", "hya.telegram"))
        st, _ = d.step(forked(st), utter("прошлый", "hya.dialog.continue", turn="s1-02"))
        _, acts = d.step(st, d.Final(now=99.0, conversation=CONV, text="Солнечно."))
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\nСолнечно.", ("Солнечно.",)), d.Say("Солнечно.")))

    def test_unclear_reply_on_the_fork_repeats_the_question(self):
        st, acts = d.step(forked(ready()), utter("что?"))
        self.assertEqual(acts, (d.Say("Начать новый диалог или продолжить прошлый?"),))


HOUR = 3600.0


def answered():
    """Ответ забран в обычной беседе; последняя реплика владельца — в 100.0."""
    st, _ = d.step(d.State(conversation=CONV), utter("какая погода", now=100.0, turn="s1-0"))
    st, _ = d.step(st, d.Final(now=101.0, conversation=CONV, text="Солнечно."))
    return st


def launch(text="", now=200.0):
    return utter(text, now=now, turn="s2-1", launch=True)


class LaunchTests(unittest.TestCase):
    def test_empty_launch_with_ready_answer_offers_it(self):
        st, acts = d.step(ready(), launch())
        self.assertEqual(acts, (d.Say("Ответ на прошлый вопрос готов. Скинуть?"),))
        self.assertEqual(st.prompt, d.READY)

    def test_empty_launch_continues_a_recent_dialog(self):
        st, acts = d.step(answered(), launch(now=100.0 + 2 * HOUR - 1))
        self.assertEqual(acts, (d.Say("Слушаю. Продолжаем прошлый разговор."),))
        self.assertEqual(st.conversation, CONV)

    def test_empty_launch_after_n_hours_starts_a_new_dialog(self):
        st, acts = d.step(answered(), launch(now=100.0 + 2 * HOUR))
        self.assertEqual(acts, (d.Say("Слушаю."),))
        self.assertEqual(st.conversation, FRESH)

    def test_first_ever_launch_is_a_new_dialog(self):
        st, acts = d.step(d.State(conversation=CONV), launch())
        self.assertEqual((acts, st.conversation), ((d.Say("Слушаю."),), FRESH))

    def test_n_hours_is_a_setting(self):
        st, acts = d.step(answered(), launch(now=100.0 + HOUR), d.Settings(continue_hours=0.5))
        self.assertEqual((acts, st.conversation), ((d.Say("Слушаю."),), FRESH))

    def test_launch_with_question_in_work_keeps_the_dialog_and_offers_to_wait(self):
        st, acts = d.step(later(), launch(now=94.0 + 3 * HOUR))
        self.assertEqual(acts, (d.Say("Ещё не готово. Подождёте?"),))
        self.assertEqual((st.conversation, st.prompt), (CONV, d.WAIT))

    def test_launch_with_a_question_is_an_ordinary_turn(self):
        st, acts = d.step(answered(), launch("а завтра?", now=150.0))
        self.assertEqual(acts, (d.Ask(CONV, "а завтра?"), d.Hold()))

    def test_launch_with_a_question_while_answer_pending_asks_new_or_old(self):
        st, acts = d.step(ready(), launch("а завтра?"))
        self.assertEqual(acts, (d.Say("Начать новый диалог или продолжить прошлый?"),))

    def test_reply_to_ready_offer(self):
        cases = (((YES, "да"), "Солнечно."), (("hya.next", "дальше"), "Солнечно."),
                 ((NO, "нет"), "Хорошо, ответ подождёт."), (("hya.later", "позже"), "Хорошо, ответ подождёт."))
        for (intent, text), heard in cases:
            with self.subTest(intent=intent):
                st, _ = d.step(ready(), launch())
                st, acts = d.step(st, utter(text, intent, now=201.0))
                self.assertEqual(acts, (d.Say(heard),))

    def test_no_on_ready_offer_keeps_the_answer(self):
        st, _ = d.step(ready(), launch())
        st, _ = d.step(st, utter("нет", NO, now=201.0))
        self.assertIsNotNone(st.answer)

    def test_phrase_on_ready_offer_asks_new_or_old(self):
        st, _ = d.step(ready(), launch())
        st, acts = d.step(st, utter("а завтра?", now=201.0))
        self.assertEqual((acts, st.held), ((d.Say("Начать новый диалог или продолжить прошлый?"),), "а завтра?"))

    def test_launch_drops_an_unresolved_fork(self):
        st, acts = d.step(forked(ready()), launch())
        self.assertEqual((acts, st.held), ((d.Say("Ответ на прошлый вопрос готов. Скинуть?"),), ""))


def sentence(i):
    return f"Пункт {i}: " + "слово " * 80 + "конец."   # ~500 символов: две в одну часть ≤ 900 не входят


S1, S2, S3 = sentence(1), sentence(2), sentence(3)


def reading(n=3, settings=d.Settings()):
    """Длинный ответ из n предложений озвучен первой частью, Алиса спросила «Продолжить?»."""
    st, _ = d.step(d.State(conversation=CONV), utter("расскажи подробно", turn="s1-0"), settings)
    st, acts = d.step(st, d.Final(now=101.0, conversation=CONV, text=" ".join(sentence(i) for i in range(1, n + 1))),
                      settings)
    assert acts == (d.Say(S1 + " Продолжить?"),), acts
    return st


class ReadingTests(unittest.TestCase):
    def test_long_answer_is_read_part_by_part(self):
        st = reading()
        self.assertEqual(st.prompt, d.CONTINUE)
        st, acts = d.step(st, utter("дальше", "hya.next", turn="s1-1"))
        self.assertEqual(acts, (d.Say(S2 + " Продолжить?"),))
        st, acts = d.step(st, utter("да", YES, turn="s1-2"))
        self.assertEqual(acts, (d.Say(S3),))
        self.assertEqual(st.prompt, d.IDLE)

    def test_no_limit_on_parts(self):
        st = reading(12)
        for i in range(2, 13):
            st, acts = d.step(st, utter("дальше", "hya.next", turn=f"s1-{i}"))
            self.assertEqual(acts, (d.Say(sentence(i) + (" Продолжить?" if i < 12 else "")),))

    def test_no_sends_the_rest_to_telegram(self):
        st, acts = d.step(reading(), utter("нет", NO))
        self.assertEqual(acts, (d.Telegram(CONV, f"Алиса:\n{S2} {S3}", (S2, S3)),
                                d.Say("Хорошо, остальное пришлю в Телеграм.")))
        self.assertEqual(st.prompt, d.IDLE)

    def test_no_without_telegram_drops_the_rest(self):
        off = d.Settings(telegram=False)
        st, acts = d.step(reading(settings=off), utter("нет", NO), off)
        self.assertEqual(acts, (d.Say("Хорошо."),))

    def test_phrase_while_reading_is_the_next_question(self):
        st, acts = d.step(reading(), utter("а короче?"))
        self.assertEqual(acts, (d.Ask(CONV, "а короче?"), d.Hold()))

    def test_repeat_while_reading_repeats_the_current_part(self):
        st, acts = d.step(reading(), utter("повтори", REPEAT))
        self.assertEqual(acts, (d.Say(S1 + " Продолжить?"),))
        st, acts = d.step(st, utter("дальше", "hya.next", turn="s1-2"))
        self.assertEqual(acts, (d.Say(S2 + " Продолжить?"),))


HELP_TEXT = ("Задайте вопрос, я передам его агенту. Пока ответ готовится, можно подождать, спросить позже или сказать "
             "„пришли в Телеграм“. „Новый диалог“ начинает разговор заново.")


class HelpRepeatTests(unittest.TestCase):
    def test_help_depends_on_what_alice_asked(self):
        offer, _ = d.step(ready(), launch())
        cases = ((answered, HELP_TEXT),
                 (waiting, "Я готовлю ответ. Можно подождать, спросить ответ позже или сказать „пришли в Телеграм“. "
                           "Подождёте?"),
                 (lambda: forked(ready()), "Скажите „новый“ или „прошлый“."),
                 (lambda: offer, HELP_TEXT + " Ответ на прошлый вопрос готов. Скинуть?"),
                 (reading, HELP_TEXT + " Продолжить?"))
        for base, heard in cases:
            with self.subTest(heard=heard[-30:]):
                st = base()
                st2, acts = d.step(st, utter("помощь", HELP, now=300.0))
                self.assertEqual(acts, (d.Say(heard),))
                self.assertEqual(st2.prompt, st.prompt)

    def test_repeat_in_plain_dialog_asks_hermes_history(self):
        st, acts = d.step(answered(), utter("повтори", REPEAT))
        self.assertEqual(acts, (d.RecallLast(CONV),))
        _, acts = d.step(st, d.Recalled(now=100.1, text="**Солнечно.** https://example.test"))
        self.assertEqual(acts, (d.Say("Солнечно."),))
        fresh, _ = d.step(d.State(conversation=CONV), utter("повтори", REPEAT))   # итога в беседе не было
        _, acts = d.step(fresh, d.Recalled(now=100.1, text=None))
        self.assertEqual(acts, (d.Say("Пока нечего повторить."),))

    def test_recalled_long_answer_is_read_by_parts(self):
        st, _ = d.step(answered(), utter("повтори", REPEAT))
        st, acts = d.step(st, d.Recalled(now=100.1, text=f"{S1} {S2}"))
        self.assertEqual((acts, st.prompt), ((d.Say(S1 + " Продолжить?"),), d.CONTINUE))

    def test_repeat_elsewhere_repeats_what_alice_said(self):
        for base, heard in ((waiting, "Вопрос принят. Спросите ответ позже или готовы подождать?"),
                            (later, "Хорошо, спросите ответ позже."),
                            (lambda: forked(ready()), "Начать новый диалог или продолжить прошлый?")):
            with self.subTest(heard=heard):
                _, acts = d.step(base(), utter("повтори", REPEAT))
                self.assertEqual(acts, (d.Say(heard),))

    def test_launch_drops_the_rest_of_reading(self):
        st, acts = d.step(reading(), launch(now=150.0))
        self.assertEqual((acts, st.reading), ((d.Say("Слушаю. Продолжаем прошлый разговор."),), ()))


GATEWAY_ERROR = "⚠️ No reply: the model didn't produce a reply."
FAILED = "Не получилось, попробуйте ещё раз."


def asked():
    st, _ = d.step(d.State(conversation=CONV), utter("какая погода", turn="s1-0"))
    return st


class FailureTests(unittest.TestCase):
    def test_answer_get_with_nothing_pending(self):
        _, acts = d.step(answered(), utter("скинь ответ", "hya.answer.get"))
        self.assertEqual(acts, (d.Say("Ответов нет, задайте вопрос."),))

    def test_details_go_to_telegram_when_voiced(self):
        _, acts = d.step(asked(), d.Final(now=101.0, conversation=CONV, text="ГОЛОС: Цена 1500.\nТЕЛЕГРАМ: Счёт №7."))
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\nСчёт №7."), d.Say("Цена 1500.")))

    def test_details_of_an_unclaimed_answer_go_to_telegram_at_once_and_only_once(self):
        # живой прогон 2026-11-18: модель не уложилась в бюджет («скинь в тг»), ТЕЛЕГРАМ-часть ждала озвучивания и не ушла
        st, acts = d.step(waiting(), d.Final(now=101.0, conversation=CONV,
                                             text="ГОЛОС: Список готов.\nТЕЛЕГРАМ: 1. Линзы в 18:30."))
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\n1. Линзы в 18:30."),))
        self.assertEqual(st.answer.parts, ("Список готов.",))
        _, acts = d.step(st, utter("да", "YANDEX.CONFIRM", turn="s1-9", now=102.0))
        self.assertEqual(acts, (d.Say("Список готов."),))   # озвучивание Telegram не повторяет

    def test_gateway_error_is_said_as_failure_and_sent_to_telegram(self):
        _, acts = d.step(asked(), d.Final(now=101.0, conversation=CONV, text=GATEWAY_ERROR))
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\n" + GATEWAY_ERROR), d.Say(FAILED)))

    def test_unclaimed_gateway_error_is_said_once_without_offer(self):
        st, acts = d.step(later(), d.Final(now=95.0, conversation=CONV, text=GATEWAY_ERROR))
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\n" + GATEWAY_ERROR),))
        for event in (utter("а завтра?"), launch()):
            with self.subTest(launch=event.launch):
                st2, acts = d.step(st, event)
                self.assertEqual(acts, (d.Say(FAILED),))
                self.assertEqual((st2.answer, st2.question, st2.prompt), (None, None, d.IDLE))
                _, acts = d.step(st2, utter("а завтра?", turn="s3-1", now=300.0))
                self.assertEqual(acts, (d.Ask(st2.conversation, "а завтра?"), d.Hold()))

    def test_turn_closed_without_final_while_waiting_is_a_failure(self):
        st, acts = d.step(asked(), d.TurnClosed(now=101.0, conversation=CONV))
        self.assertEqual(acts, (d.Say(FAILED),))
        self.assertEqual((st.question, st.prompt), (None, d.IDLE))

    def test_turn_closed_without_final_unattended_is_said_on_next_phrase(self):
        st, acts = d.step(later(), d.TurnClosed(now=95.0, conversation=CONV))
        self.assertEqual(acts, ())
        _, acts = d.step(st, utter("а завтра?"))
        self.assertEqual(acts, (d.Say(FAILED),))

    def test_turn_closed_without_final_when_telegram_was_promised(self):
        st, _ = d.step(later(), utter("пришли в телеграм", "hya.telegram"))
        st, acts = d.step(st, d.TurnClosed(now=95.0, conversation=CONV))
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\n" + FAILED),))
        _, acts = d.step(st, utter("а завтра?", turn="s1-9"))
        self.assertEqual(acts, (d.Say(FAILED),))

    def test_failed_approval_card_says_so_and_sends_the_agent_text(self):
        _, acts = d.step(asked(), d.Final(now=101.0, conversation=CONV, text="Команда отклонена.", card_failed=True))
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\nКоманда отклонена."),
                                d.Say("Запрос на подтверждение не дошёл до Телеграма, команда не выполнена.")))


LOST = "Вопрос потерялся, повторите."


def restarted(base=later, at=1000.0):
    st, acts = d.step(base(), d.GatewayStarted(now=at))
    assert acts == (), acts
    return st


class LostTests(unittest.TestCase):
    def test_question_is_lost_ten_minutes_after_restart_without_anything(self):
        _, acts = d.step(restarted(), utter("а завтра?", now=1599.0))
        self.assertEqual(acts, (d.Say("Начать новый диалог или продолжить прошлый?"),))
        st, acts = d.step(restarted(), utter("а завтра?", now=1600.0))
        self.assertEqual(acts, (d.Say(LOST),))
        self.assertIsNone(st.question)
        _, acts = d.step(st, utter("а завтра?", now=1601.0, turn="s1-9"))
        self.assertEqual(acts, (d.Ask(CONV, "а завтра?"), d.Hold()))

    def test_internal_turn_after_restart_keeps_the_question(self):
        st, _ = d.step(restarted(), d.InternalStarted(now=1004.5, conversation=CONV))
        _, acts = d.step(st, utter("скинь ответ", "hya.answer.get", now=1700.0))
        self.assertEqual(acts, (d.Hold(),))

    def test_lost_is_said_when_waiting_runs_out(self):
        st, _ = d.step(restarted(waiting), utter("да", YES, now=1598.0))
        _, acts = d.step(st, d.Expired(now=1601.5))
        self.assertEqual(acts, (d.Say(LOST),))

    def test_question_asked_after_restart_is_not_lost(self):
        st, _ = d.step(d.State(conversation=CONV), d.GatewayStarted(now=1000.0))
        st, _ = d.step(st, utter("какая погода", now=1001.0))
        st, _ = d.step(st, d.Expired(now=1004.5))
        _, acts = d.step(st, utter("да", YES, now=1700.0, turn="s1-9"))
        self.assertEqual(acts, (d.Hold(),))

    def test_lost_after_is_a_setting(self):
        _, acts = d.step(restarted(), utter("а завтра?", now=1060.0), d.Settings(lost_after=60.0))
        self.assertEqual(acts, (d.Say(LOST),))


MARK = "♻️ Recovered reply — may be a duplicate:\n\n"


def recovered(now=1100.0, conv=CONV, body="Солнечно."):
    return d.Recovered(now=now, conversation=conv, text=body, raw=MARK + body)


class RecoveredTests(unittest.TestCase):
    """HERMESYAND-4: пометка — только форма; ответом она становится при вопросе в работе и перезапуске после него."""

    def test_marked_send_without_a_restart_goes_to_telegram_and_keeps_the_question(self):
        st, acts = d.step(later(), recovered(now=100.0))
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\n" + MARK + "Солнечно."),))
        self.assertIsNotNone(st.question)
        self.assertIsNone(st.answer)

    def test_marked_send_without_a_question_goes_to_telegram(self):
        st0 = d.State(conversation=CONV)
        st, acts = d.step(st0, recovered())
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\n" + MARK + "Солнечно."),))
        self.assertEqual(st, st0)

    def test_marked_send_after_a_restart_is_the_answer_like_a_final(self):
        got = d.step(restarted(), recovered())
        want = d.step(restarted(), d.Final(now=1100.0, conversation=CONV, text="Солнечно.", internal=True))
        self.assertEqual(got, want)
        self.assertIsNone(got[0].question)
        self.assertIsNotNone(got[0].answer)

    def test_second_marked_send_after_the_answer_goes_to_telegram(self):
        st, _ = d.step(restarted(), recovered())
        _, acts = d.step(st, recovered(now=1101.0))
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\n" + MARK + "Солнечно."),))

    def test_marked_send_of_another_conversation_goes_to_telegram(self):
        st, acts = d.step(restarted(), recovered(conv=FRESH))
        self.assertEqual(acts, (d.Telegram(FRESH, "Алиса:\n" + MARK + "Солнечно."),))
        self.assertIsNotNone(st.question)

    def test_marked_send_of_an_orphan_conversation_drops_it_from_orphans(self):
        st = replace(restarted(), orphans=(FRESH, "conv-3"))
        st, _ = d.step(st, recovered(conv=FRESH))
        self.assertEqual(st.orphans, ("conv-3",))


TURN_LOST_TG ="Алиса:\nПрошлый ответ не пришёл."
MIN15 = 900.0


def started():
    """Вопрос в работе («спросите позже»), шлюз начал его ход в 95 с (on_processing_start)."""
    st, acts = d.step(later(), d.TurnAlive(now=95.0, conversation=CONV))
    assert acts == (), acts
    return st


class LostTurnTests(unittest.TestCase):
    """HERMESYAND-3: ход без on_processing_complete, без итога и без отправок старше 15 минут — потерян, закрывается тихо."""

    def test_the_question_is_closed_quietly_fifteen_minutes_after_the_turn_started(self):
        st, acts = d.step(started(), d.Tick(now=95.0 + MIN15 - 1))
        self.assertEqual((acts, st.question is not None), ((), True))
        st, acts = d.step(started(), d.Tick(now=95.0 + MIN15))
        self.assertEqual(acts, (d.DropTurn(CONV), d.Telegram(CONV, TURN_LOST_TG)))
        self.assertIsNone(st.question)
        self.assertEqual((st.announce, st.prompt, st.wait_round), ("", d.IDLE, 0))   # без озвучки

    def test_a_question_whose_turn_never_started_has_no_deadline(self):
        st, acts = d.step(later(), d.Tick(now=90.0 + 2 * HOUR))
        self.assertEqual((acts, st.question is not None), ((), True))

    def test_any_sign_of_life_moves_the_deadline(self):
        st, _ = d.step(started(), d.TurnAlive(now=500.0, conversation=CONV))
        st, acts = d.step(st, d.Tick(now=500.0 + MIN15 - 1))
        self.assertEqual(acts, ())
        _, acts = d.step(st, d.Tick(now=500.0 + MIN15))
        self.assertEqual(acts[0], d.DropTurn(CONV))

    def test_the_clock_stands_while_the_turn_waits_for_approval(self):
        st, _ = d.step(started(), d.Approval(now=100.0, conversation=CONV, waiting=True))
        st, acts = d.step(st, d.Tick(now=95.0 + 2 * HOUR))
        self.assertEqual((acts, st.question is not None), ((), True))
        st, _ = d.step(st, d.Approval(now=8000.0, conversation=CONV, waiting=False))   # нажали: отсчёт — заново
        st, acts = d.step(st, d.Tick(now=8000.0 + MIN15 - 1))
        self.assertEqual(acts, ())
        _, acts = d.step(st, d.Tick(now=8000.0 + MIN15))
        self.assertEqual(acts[0], d.DropTurn(CONV))

    def test_approval_end_does_not_start_a_clock_for_a_turn_that_never_started(self):
        st, _ = d.step(later(), d.Approval(now=100.0, conversation=CONV, waiting=False))
        _, acts = d.step(st, d.Tick(now=100.0 + MIN15))
        self.assertEqual(acts, ())

    def test_after_a_gateway_start_d9_rules_and_this_deadline_does_not(self):
        st, _ = d.step(started(), d.GatewayStarted(now=200.0))
        st, acts = d.step(st, d.Tick(now=95.0 + MIN15))
        self.assertEqual(acts, ())
        self.assertEqual(st.announce, LOST)   # Д9: 10 минут после старта

    def test_without_telegram_only_the_turn_is_dropped(self):
        st, acts = d.step(started(), d.Tick(now=95.0 + MIN15), d.Settings(telegram=False))
        self.assertEqual(acts, (d.DropTurn(CONV),))
        self.assertIsNone(st.question)

    def test_a_phrase_after_the_deadline_drops_the_turn_and_goes_to_the_agent(self):
        st, acts = d.step(started(), utter("а завтра?", now=95.0 + MIN15, turn="s1-9"))
        self.assertEqual(acts, (d.DropTurn(CONV), d.Telegram(CONV, TURN_LOST_TG), d.Ask(CONV, "а завтра?"), d.Hold()))
        self.assertIsNotNone(st.question)

    def test_a_phrase_after_the_deadline_on_wait_also_goes_to_the_agent(self):
        st, _ = d.step(waiting(), d.TurnAlive(now=95.0, conversation=CONV))
        _, acts = d.step(st, utter("а завтра?", now=95.0 + MIN15, turn="s1-9"))
        self.assertEqual(acts[2:], (d.Ask(CONV, "а завтра?"), d.Hold()))

    def test_a_request_held_when_the_turn_is_dropped_hears_a_failure_not_still_working(self):
        st, acts = d.step(started(), utter("да", YES, now=990.0, turn="s1-9"))
        self.assertEqual((acts, st.holding), ((d.Hold(),), "wait"))
        st, acts = d.step(st, d.Tick(now=95.0 + MIN15))
        self.assertEqual(acts, (d.DropTurn(CONV), d.Telegram(CONV, TURN_LOST_TG), d.Say(FAILED)))
        self.assertEqual((st.holding, st.question, st.last_reply), ("", None, FAILED))   # ретрай получит то же

    def test_sign_of_life_of_another_conversation_is_ignored(self):
        st, _ = d.step(started(), d.TurnAlive(now=500.0, conversation="old-conv"))
        _, acts = d.step(st, d.Tick(now=95.0 + MIN15))
        self.assertEqual(acts[0], d.DropTurn(CONV))


TG_FAILED = "Не смогла отправить в Телеграм. Озвучить?"
SENT = "Алиса:\nСолнечно."


def undelivered(base=answered, conversation=CONV):
    st, acts = d.step(base(), d.Delivery(now=150.0, conversation=conversation, outcome="failed", text=SENT,
                                         fallback=("Солнечно.",)))
    assert acts == (), acts
    return st


class DeliveryTests(unittest.TestCase):
    def test_failed_delivery_is_offered_on_next_phrase_and_launch(self):
        for event in (utter("а завтра?", now=160.0), launch(now=160.0)):
            with self.subTest(launch=event.launch):
                st, acts = d.step(undelivered(), event)
                self.assertEqual(acts, (d.Say(TG_FAILED),))
                self.assertEqual(st.prompt, d.TG_FAILED)

    def test_reply_to_failed_delivery_offer(self):
        cases = (((YES, "да"), (d.Say("Солнечно."),)),
                 ((NO, "нет"), (d.Say("Хорошо, ответ подождёт."),)),
                 ((None, "а послезавтра?"), (d.Say("Начать новый диалог или продолжить прошлый?"),)),
                 (("hya.telegram", "пришли в телеграм"), (d.Telegram(CONV, SENT, ("Солнечно.",)),
                                                          d.Say("Ответ пришлю в Телеграм."))))
        for (intent, text), heard in cases:
            with self.subTest(text=text):
                st, _ = d.step(undelivered(), utter("а завтра?", now=160.0))
                st, acts = d.step(st, utter(text, *([intent] if intent else []), now=161.0, turn="s1-2"))
                self.assertEqual(acts, heard)

    def test_held_phrase_is_dropped_on_yes(self):
        st, _ = d.step(undelivered(), utter("а завтра?", now=160.0))
        st, _ = d.step(st, utter("да", YES, now=161.0, turn="s1-2"))
        self.assertEqual((st.held, st.answer), ("", None))

    def test_no_keeps_the_answer_and_does_not_ask_again(self):
        st, _ = d.step(undelivered(), utter("а завтра?", now=160.0))
        st, _ = d.step(st, utter("нет", NO, now=161.0, turn="s1-2"))
        _, acts = d.step(st, utter("а завтра?", now=162.0, turn="s1-3"))
        self.assertEqual(acts, (d.Say("Начать новый диалог или продолжить прошлый?"),))

    def test_uncertain_delivery_is_mentioned_once(self):
        st, _ = d.step(answered(), d.Delivery(now=150.0, conversation=CONV, outcome="uncertain", text=SENT,
                                              fallback=("Солнечно.",)))
        st, acts = d.step(st, utter("помощь", HELP, now=160.0))
        self.assertEqual(acts, (d.Say("Возможно, ответ не дошёл до Телеграма. " + HELP_TEXT),))
        _, acts = d.step(st, utter("помощь", HELP, now=161.0, turn="s1-2"))
        self.assertEqual(acts, (d.Say(HELP_TEXT),))

    def test_failed_delivery_with_nothing_to_voice_is_silent(self):
        st, acts = d.step(answered(), d.Delivery(now=150.0, conversation=CONV, outcome="failed", text=SENT))
        self.assertEqual((acts, st.answer), ((), None))

    def test_failed_delivery_of_an_orphan_becomes_unclaimed_unless_one_is_pending(self):
        _, acts = d.step(undelivered(conversation="conv-0"), launch(now=160.0))
        self.assertEqual(acts, (d.Say(TG_FAILED),))
        st, _ = d.step(ready(), d.Delivery(now=150.0, conversation="conv-0", outcome="failed", text="Алиса:\nX",
                                           fallback=("X",)))
        self.assertEqual(st.answer.parts, ("Солнечно.",))


DAY = 86400.0


class UnclaimedTtlTests(unittest.TestCase):
    def test_unclaimed_answer_goes_to_telegram_after_a_day(self):
        st, acts = d.step(ready(), d.Tick(now=95.0 + DAY - 1))
        self.assertEqual((acts, st.answer is not None), ((), True))
        st, acts = d.step(ready(), d.Tick(now=95.0 + DAY))
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\nСолнечно."),))
        self.assertIsNone(st.answer)

    def test_expiry_also_applies_on_a_phrase(self):
        offer, _ = d.step(ready(), launch(now=200.0))
        st, acts = d.step(offer, utter("да", YES, now=95.0 + DAY, turn="s2-2"))
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\nСолнечно."), d.Ask(CONV, "да"), d.Hold()))

    def test_undelivered_answer_is_not_sent_again(self):
        st, acts = d.step(undelivered(), d.Tick(now=150.0 + DAY))
        self.assertEqual((acts, st.answer), ((), None))

    def test_without_telegram_it_is_just_deleted(self):
        st, acts = d.step(ready(), d.Tick(now=95.0 + DAY), d.Settings(telegram=False))
        self.assertEqual((acts, st.answer), ((), None))

    def test_ttl_is_a_setting(self):
        _, acts = d.step(ready(), d.Tick(now=95.0 + 60), d.Settings(unclaimed_ttl=60.0))
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\nСолнечно."),))


def talked() -> d.State:
    """Беседа после озвученного ответа, читаемого по частям: тексты лежат в состоянии, вопроса и ответа нет."""
    return d.State(conversation=CONV, prompt=d.CONTINUE, held="фраза", last_owner_at=100.0,
                   reading=("Часть 1.", "Часть 2."), reading_pos=1, last_said="Продолжить?", last_turn="s1-5",
                   last_reply="Продолжить?", answered=True)


class TextRetentionTests(unittest.TestCase):
    """Решение владельца 2026-11-17 (этап 4): тексты беседы хранятся до 24 ч тишины владельца."""

    def test_texts_are_wiped_after_a_day_of_silence(self):
        st, acts = d.step(talked(), d.Tick(now=100.0 + DAY))
        self.assertEqual(acts, ())
        self.assertEqual((st.last_said, st.last_reply, st.last_turn, st.reading, st.reading_pos, st.held, st.prompt),
                         ("", "", "", (), 0, "", d.IDLE))

    def test_pending_phrases_are_wiped_too(self):
        st, _ = d.step(replace(talked(), announce="Не получилось, попробуйте ещё раз.", notice="Предупреждение."),
                       d.Tick(now=100.0 + DAY))
        self.assertEqual((st.announce, st.notice), ("", ""))

    def test_texts_are_kept_until_the_day_is_over(self):
        self.assertEqual(d.step(talked(), d.Tick(now=100.0 + DAY - 1)), (talked(), ()))

    def test_texts_are_kept_while_a_question_is_in_work(self):
        busy = replace(talked(), question=d.Question(asked_at=100.0))
        st, _ = d.step(busy, d.Tick(now=100.0 + DAY))
        self.assertEqual((st.last_said, st.reading), ("Продолжить?", ("Часть 1.", "Часть 2.")))


REJECTED_TEXTS = ("/new", "а" * 1001, "стоп\x01")
REJECT = "Такую команду я не передам."


class GuardTests(unittest.TestCase):
    def test_rejected_phrase_depends_on_what_alice_asked(self):
        offer, _ = d.step(ready(), launch())
        cases = ((answered, (d.Say(REJECT),)), (waiting, (d.Hold(),)),
                 (lambda: forked(ready()), (d.Say("Начать новый диалог или продолжить прошлый?"),)),
                 (lambda: offer, (d.Say(REJECT),)))
        for base, heard in cases:
            for text in REJECTED_TEXTS:
                with self.subTest(heard=heard, text=text[:10]):
                    st = base()
                    st2, acts = d.step(st, utter(text, now=300.0))
                    self.assertEqual(acts, heard)
                    self.assertEqual(st2.prompt, st.prompt)

    def test_rate_limit_counts_only_agent_turns(self):
        st = d.State(conversation=CONV)
        for i in range(6):
            st, acts = d.step(st, utter(f"вопрос {i}", now=100.0 + i, turn=f"s1-{i}"))
            self.assertEqual(acts[0], d.Ask(CONV, f"вопрос {i}"))
            st, _ = d.step(st, d.Final(now=100.5 + i, conversation=CONV, text="Ок."))
            st, _ = d.step(st, utter("помощь", HELP, now=100.6 + i, turn=f"s1-{i}h"))
        st, acts = d.step(st, utter("ещё", now=106.0, turn="s1-7"))
        self.assertEqual(acts, (d.Say("Слишком много команд подряд, подождите минуту."),))
        self.assertIsNone(st.question)
        _, acts = d.step(st, utter("ещё", now=160.0, turn="s1-8"))
        self.assertEqual(acts, (d.Ask(CONV, "ещё"), d.Hold()))

    def test_rate_limit_is_a_setting(self):
        st, _ = d.step(d.State(conversation=CONV), utter("раз", now=100.0), d.Settings(rate_per_minute=1))
        st, _ = d.step(st, d.Final(now=100.5, conversation=CONV, text="Ок."), d.Settings(rate_per_minute=1))
        _, acts = d.step(st, utter("два", now=101.0, turn="s1-2"), d.Settings(rate_per_minute=1))
        self.assertEqual(acts, (d.Say("Слишком много команд подряд, подождите минуту."),))

    def test_yandex_retry_gets_the_same_reply_without_side_effects(self):
        st, _ = d.step(answered(), utter("скинь ответ", "hya.answer.get", turn="s1-5"))
        st2, acts = d.step(st, utter("скинь ответ", "hya.answer.get", turn="s1-5"))
        self.assertEqual((acts, st2), ((d.Say("Ответов нет, задайте вопрос."),), st))
        st, _ = d.step(answered(), utter("а завтра?", turn="s1-5"))
        st, acts = d.step(st, d.Final(now=101.0, conversation=CONV, text="Дождь."))
        _, acts = d.step(st, utter("а завтра?", turn="s1-5"))
        self.assertEqual(acts, (d.Say("Дождь."),))

    def test_yandex_retry_while_holding_holds_again(self):
        st, _ = d.step(answered(), utter("а завтра?", turn="s1-5"))
        st2, acts = d.step(st, utter("а завтра?", turn="s1-5", now=104.0))
        self.assertEqual((acts, st2), ((d.Hold(),), st))

    def test_empty_phrase_outside_launch(self):
        for base, heard in ((answered, (d.Say("Слушаю."),)), (waiting, (d.Hold(),)),
                            (later, (d.Say("Хорошо, спросите ответ позже."),))):
            with self.subTest(heard=heard):
                _, acts = d.step(base(), utter(""))
                self.assertEqual(acts, heard)


OFF = d.Settings(telegram=False)


class NoTelegramTests(unittest.TestCase):
    """П-OFF: Telegram не настроен — отправок нет, голос этого не обещает."""

    def test_send_to_telegram_means_later(self):
        _, acts = d.step(waiting(), utter("пришли в телеграм", "hya.telegram"), OFF)
        self.assertEqual(acts, (d.Say("Хорошо, спросите ответ позже."),))
        _, acts = d.step(ready(), utter("пришли в телеграм", "hya.telegram"), OFF)
        self.assertEqual(acts, (d.Say("Хорошо, ответ подождёт."),))

    def test_new_dialog_drops_the_pending_answer(self):
        st, acts = d.step(ready(), utter("новый диалог", "hya.dialog.new"), OFF)
        self.assertEqual((acts, st.answer), ((d.Say("Слушаю."),), None))

    def test_nothing_goes_to_telegram(self):
        st, _ = d.step(later(), utter("а завтра?"), OFF)
        st, _ = d.step(st, utter("новый", "hya.dialog.new", turn="s1-2"), OFF)
        _, acts = d.step(st, d.Final(now=99.0, conversation=CONV, text="Солнечно."), OFF)
        self.assertEqual(acts, ())


class ReviewFixTests(unittest.TestCase):
    """Исправления по /code-review medium 2026-11-17 (решения владельца)."""

    def test_telegram_only_answer_is_voiced_without_telegram(self):
        st, _ = d.step(d.State(conversation=CONV), utter("какая погода"), OFF)
        _, acts = d.step(st, d.Final(now=101.0, conversation=CONV, text="ТЕЛЕГРАМ: Солнечно, **+20**."), OFF)
        self.assertEqual(acts, (d.Say("Солнечно, +20."),))

    def test_repeat_of_gateway_error_or_telegram_only_answer(self):
        st, _ = d.step(answered(), utter("повтори", REPEAT))
        _, acts = d.step(st, d.Recalled(now=100.1, text=GATEWAY_ERROR))
        self.assertEqual(acts, (d.Say(FAILED),))
        _, acts = d.step(st, d.Recalled(now=100.1, text="ТЕЛЕГРАМ: Солнечно."))
        self.assertEqual(acts, (d.Say("Солнечно."),))

    def test_retry_while_recalling_asks_history_again(self):
        st, _ = d.step(answered(), utter("повтори", REPEAT, turn="s1-5"))
        _, acts = d.step(st, utter("повтори", REPEAT, turn="s1-5"))
        self.assertEqual(acts, (d.RecallLast(CONV),))

    def test_new_dialog_drops_an_undelivered_answer(self):
        st, _ = d.step(undelivered(), utter("а завтра?", now=160.0))
        st, _ = d.step(st, utter("нет", NO, now=161.0, turn="s1-2"))
        st, acts = d.step(st, utter("новый диалог", "hya.dialog.new", now=162.0, turn="s1-3"))
        self.assertEqual((acts, st.answer), ((d.Say("Слушаю."),), None))

    def test_yes_no_next_with_ready_answer_and_no_offer(self):
        for (intent, text), heard in (((YES, "да"), "Солнечно."), (("hya.next", "дальше"), "Солнечно."),
                                      ((NO, "нет"), "Хорошо, ответ подождёт.")):
            with self.subTest(text=text):
                _, acts = d.step(ready(), utter(text, intent))
                self.assertEqual(acts, (d.Say(heard),))

    def test_new_with_held_phrase_mentions_telegram_once(self):
        st, acts = d.step(forked(ready()), utter("новый", "hya.dialog.new"))
        st, acts = d.step(st, d.Final(now=97.0, conversation=FRESH, text="Дождь."))
        self.assertEqual(acts, (d.Say("Ответ пришлю в Телеграм. Дождь."),))
        st, _ = d.step(st, utter("а потом?", turn="s1-9", now=98.0))
        _, acts = d.step(st, d.Final(now=99.0, conversation=FRESH, text="Снег."))
        self.assertEqual(acts, (d.Say("Снег."),))

    def test_fresh_final_wins_over_an_undelivered_answer(self):   # принятый предел: один слот незабранного
        st, _ = d.step(later(), d.Delivery(now=95.0, conversation=CONV, outcome="failed", text="Алиса:\nX",
                                           fallback=("X",)))
        st, _ = d.step(st, d.Final(now=96.0, conversation=CONV, text="Солнечно."))
        self.assertEqual((st.answer.parts, st.answer.undelivered), (("Солнечно.",), False))


class RecallTests(unittest.TestCase):
    """S7: «повтори» из истории Hermes — сбой чтения и срок (docs/PLAN.md, этап 2, срез «б», Р-S7-1…3)."""

    def test_expired_while_recalling_says_failed(self):
        st, acts = d.step(answered(), utter("повтори", REPEAT))
        self.assertEqual(acts, (d.RecallLast(CONV),))
        st, acts = d.step(st, d.Expired(now=103.5))
        self.assertEqual(acts, (d.Say(FAILED),))
        self.assertEqual((st.holding, st.prompt), ("", d.IDLE))

    def test_late_recall_after_expired_is_ignored(self):
        st, _ = d.step(answered(), utter("повтори", REPEAT))
        st, _ = d.step(st, d.Expired(now=103.5))
        st2, acts = d.step(st, d.Recalled(now=104.0, text="Солнечно."))
        self.assertEqual((acts, st2), ((), st))

    def test_read_error_says_failed_and_repeat_reads_again(self):
        st, _ = d.step(answered(), utter("повтори", REPEAT, turn="s1-5"))
        st, acts = d.step(st, d.Recalled(now=100.1, text=None, outcome=d.RECALL_ERROR))
        self.assertEqual(acts, (d.Say(FAILED),))
        _, acts = d.step(st, utter("повтори", REPEAT, turn="s1-6", now=101.0))
        self.assertEqual(acts, (d.RecallLast(CONV),))

    def test_missing_or_textless_after_an_answer_is_a_failure(self):
        for outcome in (d.RECALL_MISSING, d.RECALL_FOUND):
            with self.subTest(outcome=outcome):
                st, _ = d.step(answered(), utter("повтори", REPEAT))
                _, acts = d.step(st, d.Recalled(now=100.1, text=None, outcome=outcome))
                self.assertEqual(acts, (d.Say(FAILED),))

    def test_repeat_after_a_failed_turn_says_failed(self):   # S7, Р-S7-4: в истории — заглушка (empty), не «No reply…»
        st, _ = d.step(answered(), utter("повтори", REPEAT))
        _, acts = d.step(st, d.Recalled(now=100.1, text="(empty)"))
        self.assertEqual(acts, (d.Say(FAILED),))

    def test_repeat_after_a_gateway_stop_mid_turn_says_failed(self):   # S7: в истории — «Operation interrupted: …»
        st, _ = d.step(answered(), utter("повтори", REPEAT))
        _, acts = d.step(st, d.Recalled(now=100.1, text="Operation interrupted: waiting for model response (0.6s elapsed)."))
        self.assertEqual(acts, (d.Say(FAILED),))

    def test_missing_in_a_new_dialog_is_nothing_to_repeat(self):
        new, _ = d.step(answered(), utter("новый диалог", "hya.dialog.new", now=110.0, turn="s1-7"))
        fresh_launch = utter("повтори", REPEAT, now=100.0 + 3 * HOUR, turn="s2-1", launch=True)
        for name, st, event, conv in (("новый диалог", new, utter("повтори", REPEAT, now=111.0, turn="s1-8"), FRESH),
                                      ("свежий запуск", answered(), fresh_launch, FRESH)):
            with self.subTest(name):
                st, acts = d.step(st, event)
                self.assertEqual(acts, (d.RecallLast(conv),))
                _, acts = d.step(st, d.Recalled(now=200.0, text=None, outcome=d.RECALL_MISSING))
                self.assertEqual(acts, (d.Say("Пока нечего повторить."),))

    def test_late_recall_after_new_dialog_or_gateway_restart_is_ignored(self):
        reading_history, _ = d.step(answered(), utter("повтори", REPEAT, turn="s1-5"))
        for name, event in (("новый диалог", utter("новый диалог", "hya.dialog.new", now=101.0, turn="s1-6")),
                            ("перезапуск шлюза", d.GatewayStarted(now=101.0))):
            with self.subTest(name):
                st, _ = d.step(reading_history, event)
                st2, acts = d.step(st, d.Recalled(now=101.5, text="Солнечно."))
                self.assertEqual((acts, st2), ((), st))


class LaunchKeepsPendingTests(unittest.TestCase):
    def test_launch_after_n_hours_with_unclaimed_answer_keeps_the_dialog(self):   # И4
        st, acts = d.step(ready(), launch(now=95.0 + 3 * HOUR))
        self.assertEqual((acts, st.conversation), ((d.Say("Ответ на прошлый вопрос готов. Скинуть?"),), CONV))


class PriorityTests(unittest.TestCase):
    def test_own_intents_win_over_yandex_ones(self):
        st, acts = d.step(waiting(), utter("нет, пришли в телеграм", NO, "hya.telegram"))
        self.assertEqual(acts, (d.Say("Ответ пришлю в Телеграм."),))
        offer, _ = d.step(ready(), launch())
        st, acts = d.step(offer, utter("да, новый диалог", YES, "hya.dialog.new", now=201.0))
        self.assertEqual(st.conversation, FRESH)

    def test_full_order(self):
        order = ["hya.dialog.new", "hya.telegram", "hya.answer.get", "hya.dialog.continue", "hya.next", "hya.later",
                 "hya.wait", HELP, REPEAT, YES, NO]
        for i, name in enumerate(order):
            with self.subTest(name=name):
                self.assertEqual(d.kind_of("фраза", tuple(reversed(order[i:]))), name)


def offered():
    st, _ = d.step(ready(), launch())
    return st


def tg_offered():
    st, _ = d.step(undelivered(), utter("а завтра?", now=160.0, turn="s1-77"))
    return st


def ready_on_wait():
    st, _ = d.step(waiting(), d.Final(now=95.0, conversation=CONV, text="Солнечно."))
    return st


COLUMNS = {  # реплика владельца: (текст, интенты)
    "да": ("да", (YES,)), "нет": ("нет", (NO,)), "без интента": ("а завтра?", ()), "пусто": ("", ()),
    "отклонённая": ("/new", ()), "скинь": ("скинь ответ", ("hya.answer.get",)),
    "прошлый": ("прошлый", ("hya.dialog.continue",)), "новый": ("новый диалог", ("hya.dialog.new",)),
    "подожду": ("подожду", ("hya.wait",)), "позже": ("позже", ("hya.later",)),
    "в телеграм": ("пришли в телеграм", ("hya.telegram",)), "дальше": ("дальше", ("hya.next",)),
    "помощь": ("помощь", (HELP,)), "повтори": ("повтори", (REPEAT,)),
}
LATER_T, ACCEPTED_T = "Хорошо, спросите ответ позже.", "Вопрос принят. Спросите ответ позже или готовы подождать?"
NOO_T, SAY_NOO_T, WAITS_T = "Начать новый диалог или продолжить прошлый?", "Скажите „новый“ или „прошлый“.", \
    "Хорошо, ответ подождёт."
READY_T, IN_TG_T, NEW_TG_T = "Ответ на прошлый вопрос готов. Скинуть?", "Ответ пришлю в Телеграм.", \
    "Ответ пришлю в Телеграм. Слушаю."
WAIT_HELP_T = "Я готовлю ответ. Можно подождать, спросить ответ позже или сказать „пришли в Телеграм“. Подождёте?"
ANS = ("Солнечно.",)
VOICE, HOLD, FORK = (d.Say("Солнечно."),), (d.Hold(),), (d.Say(NOO_T),)
TG_ANS = (d.Telegram(CONV, SENT, ANS), d.Say(IN_TG_T))
NEW_A = (d.Telegram(CONV, SENT, ANS), d.Say(NEW_TG_T))
REST = (d.Telegram(CONV, f"Алиса:\n{S2} {S3}", (S2, S3)), d.Say("Хорошо, остальное пришлю в Телеграм."))
NEXT = (d.Say(S2 + " Продолжить?"),)


def say(text):
    return (d.Say(text),)


def ask(text):
    return (d.Ask(CONV, text), d.Hold())


# Таблица «последний вопрос Алисы × реплика» (docs/PLAN.md, этап 2, срез «б»): каждая клетка — литерал.
TABLE = {
    "беседа": (answered, {
        "да": ask("да"), "нет": ask("нет"), "без интента": ask("а завтра?"), "пусто": say("Слушаю."),
        "отклонённая": say(REJECT), "скинь": say("Ответов нет, задайте вопрос."), "прошлый": ask("прошлый"),
        "новый": say("Слушаю."), "подожду": ask("подожду"), "позже": ask("позже"),
        "в телеграм": ask("пришли в телеграм"), "дальше": ask("дальше"), "помощь": say(HELP_TEXT),
        "повтори": (d.RecallLast(CONV),)}),
    "вопрос в работе": (later, {
        "да": HOLD, "нет": say(LATER_T), "без интента": FORK, "пусто": say(LATER_T), "отклонённая": say(REJECT),
        "скинь": HOLD, "прошлый": HOLD, "новый": say(NEW_TG_T), "подожду": HOLD, "позже": say(LATER_T),
        "в телеграм": say(IN_TG_T), "дальше": FORK, "помощь": say(HELP_TEXT), "повтори": say(LATER_T)}),
    "ответ готов": (ready, {
        "да": VOICE, "нет": say(WAITS_T), "без интента": FORK, "пусто": say(LATER_T), "отклонённая": say(REJECT),
        "скинь": VOICE, "прошлый": VOICE, "новый": NEW_A, "подожду": VOICE, "позже": say(WAITS_T),
        "в телеграм": TG_ANS, "дальше": VOICE, "помощь": say(HELP_TEXT), "повтори": say(LATER_T)}),
    "подождёте?": (waiting, {
        "да": HOLD, "нет": say(LATER_T), "без интента": HOLD, "пусто": HOLD, "отклонённая": HOLD, "скинь": HOLD,
        "прошлый": HOLD, "новый": say(NEW_TG_T), "подожду": HOLD, "позже": say(LATER_T), "в телеграм": say(IN_TG_T),
        "дальше": HOLD, "помощь": say(WAIT_HELP_T), "повтори": say(ACCEPTED_T)}),
    "подождёте? + готово": (ready_on_wait, {
        "да": VOICE, "нет": say(WAITS_T), "без интента": VOICE, "пусто": VOICE, "отклонённая": VOICE,
        "скинь": VOICE, "прошлый": VOICE, "новый": NEW_A, "подожду": VOICE, "позже": say(WAITS_T),
        "в телеграм": TG_ANS, "дальше": VOICE, "помощь": say(WAIT_HELP_T), "повтори": say(ACCEPTED_T)}),
    "новый или прошлый? + вопрос": (lambda: forked(later()), {
        "да": say(SAY_NOO_T), "нет": say(SAY_NOO_T), "без интента": FORK, "пусто": FORK, "отклонённая": FORK,
        "скинь": HOLD, "прошлый": HOLD, "новый": (d.Ask(FRESH, "а завтра?"), d.Hold()), "подожду": FORK,
        "позже": FORK, "в телеграм": say(SAY_NOO_T), "дальше": FORK, "помощь": say(SAY_NOO_T), "повтори": FORK}),
    "новый или прошлый? + ответ": (lambda: forked(ready()), {
        "да": say(SAY_NOO_T), "нет": say(SAY_NOO_T), "без интента": FORK, "пусто": FORK, "отклонённая": FORK,
        "скинь": VOICE, "прошлый": VOICE, "новый": (d.Telegram(CONV, SENT, ANS), d.Ask(FRESH, "а завтра?"), d.Hold()),
        "подожду": FORK, "позже": FORK, "в телеграм": say(SAY_NOO_T), "дальше": FORK, "помощь": say(SAY_NOO_T),
        "повтори": FORK}),
    "скинуть?": (offered, {
        "да": VOICE, "нет": say(WAITS_T), "без интента": FORK, "пусто": say(READY_T), "отклонённая": say(REJECT),
        "скинь": VOICE, "прошлый": VOICE, "новый": NEW_A, "подожду": VOICE, "позже": say(WAITS_T),
        "в телеграм": TG_ANS, "дальше": VOICE, "помощь": say(HELP_TEXT + " " + READY_T), "повтори": say(READY_T)}),
    "озвучить?": (tg_offered, {
        "да": VOICE, "нет": say(WAITS_T), "без интента": FORK, "пусто": say(TG_FAILED), "отклонённая": say(REJECT),
        "скинь": VOICE, "прошлый": VOICE, "новый": (d.Ask(FRESH, "а завтра?"), d.Hold()),
        "подожду": VOICE, "позже": say(WAITS_T), "в телеграм": TG_ANS, "дальше": VOICE,
        "помощь": say(HELP_TEXT + " " + TG_FAILED), "повтори": say(TG_FAILED)}),
    "продолжить?": (reading, {
        "да": NEXT, "нет": REST, "без интента": ask("а завтра?"), "пусто": say("Продолжить?"),
        "отклонённая": say(REJECT), "скинь": NEXT, "прошлый": NEXT,
        "новый": (d.Telegram(CONV, f"Алиса:\n{S2} {S3}", (S2, S3)), d.Say(NEW_TG_T)), "подожду": NEXT,
        "позже": REST, "в телеграм": REST, "дальше": NEXT, "помощь": say(HELP_TEXT + " Продолжить?"),
        "повтори": say(S1 + " Продолжить?")}),
}


class TableTests(unittest.TestCase):
    """Регресс поверх срезов: каждая клетка таблицы и инварианты И1–И3 на каждой клетке."""

    def test_every_cell(self):
        for row, (base, cells) in TABLE.items():
            self.assertEqual(set(cells), set(COLUMNS), row)
            for col, expected in cells.items():
                text, intents = COLUMNS[col]
                with self.subTest(row=row, col=col):
                    _, acts = d.step(base(), utter(text, *intents, now=300.0, turn="s9-1"))
                    self.assertEqual(acts, expected)

    def test_invariants_on_every_cell(self):
        replies = (d.Say, d.Hold, d.RecallLast)
        for row, (base, cells) in TABLE.items():
            for col in cells:
                text, intents = COLUMNS[col]
                with self.subTest(row=row, col=col):
                    st = base()
                    _, acts = d.step(st, utter(text, *intents, now=300.0, turn="s9-1"))
                    # И2: на реплику — ровно одно из «сказать» / «ждать» / «взять из истории»
                    self.assertEqual(sum(isinstance(a, replies) for a in acts), 1, acts)
                    # И3: ответ Алисе укладывается в лимит response.text
                    self.assertTrue(all(len(a.text) <= 1024 for a in acts if isinstance(a, d.Say)))
                    # И1 (Р2): ход агенту — только в беседу без вопроса в работе
                    for a in acts:
                        if isinstance(a, d.Ask):
                            self.assertFalse(st.question is not None and a.conversation == st.conversation)


APPROVALS_OFF = "Эту команду нужно подтвердить в Телеграме, а подтверждения не настроены. Команда не выполнена."


class ApprovalsOffTests(unittest.TestCase):
    """Подтверждения недоступны заранее (этап 5, срез «б», решение владельца 2026-11-17): свой текст голосом, не «не
    дошёл до Телеграма»; ответ агента — в Telegram, если он есть, иначе отбрасывается."""

    def off_final(self, now=101.0):
        return d.Final(now=now, conversation=CONV, text="Команда отклонена.", approvals_off=True)

    def test_held_reply_says_approvals_are_off_and_sends_the_agent_text(self):
        _, acts = d.step(asked(), self.off_final())
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\nКоманда отклонена."), d.Say(APPROVALS_OFF)))

    def test_unattended_it_is_said_once_on_the_next_reply(self):
        st, acts = d.step(later(), self.off_final(now=95.0))
        self.assertEqual(acts, (d.Telegram(CONV, "Алиса:\nКоманда отклонена."),))
        st, acts = d.step(st, utter("ну что?", turn="s1-5"))
        self.assertEqual(acts, (d.Say(APPROVALS_OFF),))
        self.assertIsNone(st.answer)

    def test_without_telegram_only_the_voice_remains(self):
        _, acts = d.step(asked(), self.off_final(), OFF)
        self.assertEqual(acts, (d.Say(APPROVALS_OFF),))


if __name__ == "__main__":
    unittest.main()
