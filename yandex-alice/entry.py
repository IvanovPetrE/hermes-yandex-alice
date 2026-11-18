"""Порядок проверок HTTP-входа (docs/DESIGN.md, этап 3, срез «б», б1) — без Hermes и без aiohttp.

Адаптер зовёт `check_path(tail, cfg)` ДО чтения тела (иначе 413 пришёл бы на неверный путь раньше 404), затем читает тело
с лимитом `protocol.MAX_BODY` и зовёт `screen(body, cfg, on_unbound)`. Порядок: путь → размер → формат → `ping` → skill_id →
привязка/владелец. Отказы по Д16: HTTP 200, короткий текст, `end_session: true`; ошибки пути, формата и размера — HTTP
404 / 400 / 413 без тела. Отдельного лимита частоты нет: Д16 (6 ходов агента в минуту) считает `dialog.step`.
Секреты и тексты реплик не логируются; единственный ID в логе — user_id в режиме привязки (`OnceNote`).
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Callable

try:
    from . import protocol as p
except ImportError:  # тесты импортируют модули плагина как модули верхнего уровня
    import protocol as p  # type: ignore[no-redef]


@dataclass(frozen=True)
class EntryConfig:
    secret: str            # секретная часть пути webhook
    skill_id: str          # ожидаемый session.skill_id
    owner_user_id: str     # "" — режим привязки


@dataclass(frozen=True)
class Reject:
    """HTTP-ошибка без тела: 404 / 400 / 413."""
    status: int


@dataclass(frozen=True)
class Reply:
    """Ответ Алисе без хода агента (HTTP 200): pong или отказ."""
    body: bytes


@dataclass(frozen=True)
class Passed:
    """Владелец: запрос идёт в беседу."""
    request: p.AliceRequest


def screen(body: bytes, cfg: EntryConfig, on_unbound: Callable[[str], None] | None = None) -> Reject | Reply | Passed:
    if len(body) > p.MAX_BODY:
        return Reject(413)
    try:
        request = p.parse_request(body)
    except p.BadRequest:
        return Reject(400)
    if request.text.lower() == "ping":   # первым после формата: есть ли в ping session.user, не документировано
        return Reply(p.alice_reply(p.REPLY_PING))
    refusal = Reply(p.alice_reply(p.REPLY_NOT_OWNER, end_session=True))
    if request.skill_id != cfg.skill_id:   # skill_id подделываем — второй рубеж после секретного пути
        return refusal
    if not cfg.owner_user_id:   # режим привязки: никого не пускаем, владелец узнаёт свой user_id из лога
        if on_unbound is not None:
            on_unbound(request.user_id)
        return Reply(p.alice_reply(p.REPLY_UNBOUND, end_session=True))
    if not p.is_owner(request.user_id, cfg.owner_user_id):
        return refusal
    return Passed(request)


class OnceNote:
    """Подсказка режима привязки: user_id запроса — в лог не чаще раза за жизнь объекта (за запуск шлюза)."""

    def __init__(self, log: Callable[[str], None]):
        self._log = log
        self._done = False

    def __call__(self, user_id: str) -> None:
        if self._done:
            return
        self._done = True
        self._log("unbound: ALICE_OWNER_USER_ID не задан; user_id запроса: "
                  + (user_id or "<пользователь не авторизован в Алисе>"))


def check_path(tail: str, cfg: EntryConfig) -> bool:
    return bool(cfg.secret) and hmac.compare_digest(tail.encode(), cfg.secret.encode())
