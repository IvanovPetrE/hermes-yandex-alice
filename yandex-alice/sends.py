"""Что такое отправка шлюза для голосовой платформы — без зависимостей от Hermes.

Перенесено из проб S1/S5/S6 (`prototypes/s6/plugin/recovery.py`): правило итога хода S1 (`reply_to` == `message_id`
открытого хода и `notify is True`, без `_interim_send`; префиксы `⚡ ⏳ ⏩ 💾` итог не отменяют), пометка ответа из
леджера «♻️ …:» — срезается по форме, слова не разбираются (S5), и пересылка в Telegram того, что пришло вне хода (S6).
Что делать с итогом и восстановленным ответом — dialog.py; кто открыл и закрыл ход — адаптер.
"""
from __future__ import annotations

try:
    from . import protocol
except ImportError:  # тесты импортируют модули плагина как модули верхнего уровня
    import protocol  # type: ignore[no-redef]

FINAL, INTERIM, OUTSIDE, RECOVERED, LATE = "final", "interim", "outside", "recovered", "late"
_SIGN = "♻"   # Hermes дописывает U+FE0F, его не требуем


def split_marker(text: str | None) -> tuple[str, str]:
    """(пометка, остальное); ("", text), если первый абзац не похож на пометку."""
    text = text or ""
    head, sep, rest = text.partition("\n\n")
    if sep and rest.strip() and head.startswith(_SIGN) and head.rstrip().endswith(":"):
        return head, rest
    return "", text


def strip_marker(text: str | None) -> str:
    return split_marker(text)[1]


def classify_send(*, open_turn: bool, turn_mid: str | None, reply_to: str | None, notify: object,
                  interim: bool, text: str | None, approval_prompt: bool = False, late: bool = False,
                  asked_mid: str | None = None) -> str:
    """FINAL / INTERIM / RECOVERED / LATE / OUTSIDE для одного send().

    open_turn — у чата есть ход между on_processing_start и on_processing_complete; turn_mid — message_id этого хода
    (None у внутреннего хода автопродолжения, его итог приходит с reply_to=None); approval_prompt — текст `/approve`
    Hermes (`is_approval_prompt`): служебный, как промежуточная отправка (S4, вывод 5); late — reply_to — ход, снятый
    без complete (HERMESYAND-3): его итог — поздний, при любом открытом ходе; asked_mid — message_id фразы текущего
    вопроса, у которой не было on_processing_start (HERMESYAND-26: busy_input_mode interrupt перенаправляет ход без
    хуков): итог с её reply_to и notify=True — итог хода, подсказка занятой сессии с тем же reply_to — служебная."""
    if interim or approval_prompt:
        return INTERIM
    if open_turn and notify is True and reply_to == turn_mid:
        return FINAL
    if late and notify is True:
        return LATE
    if not open_turn and asked_mid is not None and reply_to == asked_mid:
        return FINAL if notify is True else INTERIM
    if not open_turn and split_marker(text)[0]:
        return RECOVERED
    return OUTSIDE


def forward_text(*, open_turn: bool, kind: str, text: str | None) -> str | None:
    """Текст для Telegram у отправки вне хода, которая не служебная и не восстановленная; иначе None.
    Неитоговое внутри хода (📬 No home channel…, прогресс, повторы) не озвучивается и не пересылается.
    Поздний итог снятого хода (LATE) — весь, с подписью, при любом открытом ходе."""
    if kind == LATE:
        return protocol.whole_text(text or "") if (text or "").strip() else None
    if open_turn or kind != OUTSIDE or not (text or "").strip():
        return None
    return protocol.telegram_text(text or "")
