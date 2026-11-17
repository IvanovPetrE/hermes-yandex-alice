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

FINAL, INTERIM, OUTSIDE, RECOVERED = "final", "interim", "outside", "recovered"
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
                  interim: bool, text: str | None) -> str:
    """FINAL / INTERIM / RECOVERED / OUTSIDE для одного send().

    open_turn — у чата есть ход между on_processing_start и on_processing_complete; turn_mid — message_id этого хода
    (None у внутреннего хода автопродолжения, его итог приходит с reply_to=None)."""
    if interim:
        return INTERIM
    if open_turn and notify is True and reply_to == turn_mid:
        return FINAL
    if not open_turn and split_marker(text)[0]:
        return RECOVERED
    return OUTSIDE


def forward_text(*, open_turn: bool, kind: str, text: str | None) -> str | None:
    """Текст для Telegram у отправки вне хода, которая не служебная и не восстановленная; иначе None.
    Неитоговое внутри хода (📬 No home channel…, прогресс, повторы) не озвучивается и не пересылается."""
    if open_turn or kind != OUTSIDE or not (text or "").strip():
        return None
    return protocol.telegram_text(text or "")
