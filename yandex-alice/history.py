"""Правило «последнего ответа» из истории Hermes (для «повтори») — без зависимостей от Hermes.

Перенесено из пробы S7 (`prototypes/s7/plugin/history.py`): `last_answer`, `read_tail`, `pick_profile`. Здесь только
чистая логика; цепочка чтения (маршруты → профиль → дом → `SessionDB(read_only=True)` → `find_session_by_origin` →
`resolve_resume_session_id` → `get_messages`) и блокировка «одно чтение за раз» — в адаптере (этап 3, срез «б»).
`read_tail` работает с любым объектом, у которого есть `get_messages(session_id, latest, limit, offset)`.
"""

TAIL = 20          # сообщений в порции
MAX_PORTIONS = 5   # порций за одно чтение


def last_answer(rows):
    """Новейший ответ агента С ТЕКСТОМ в `rows` (старые первыми) или `None`.

    Ответ — сообщение `assistant` с непустым строковым `content`, без `tool_calls` (комментарий рядом с вызовом
    инструмента ответом не считается) и не сводка сжатия.
    """
    for row in reversed(rows or []):
        if not isinstance(row, dict) or row.get("role") != "assistant":
            continue
        if row.get("tool_calls") or row.get("_compressed_summary"):
            continue
        content = row.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return None


def read_tail(db, session_id, tail=TAIL, portions=MAX_PORTIONS):
    """`(текст или None, прочитано порций)`: хвост порциями, новейшие первыми, пока не найден ответ."""
    read = 0
    for i in range(portions):
        rows = db.get_messages(session_id, latest=True, limit=tail, offset=i * tail)
        read += 1
        text = last_answer(rows)
        if text is not None:
            return text, read
        if len(rows) < tail:
            break
    return None, read


def pick_profile(routes, platform):
    """Целевой профиль платформы по маршрутам шлюза: первый включённый маршрут этой платформы без различителей
    (chat / thread / guild / user); `"default"`, если такого нет."""
    for route in routes or []:
        if getattr(route, "platform", None) != platform or not getattr(route, "enabled", True):
            continue
        if any(getattr(route, name, None) for name in ("chat_id", "thread_id", "guild_id", "user_id")):
            continue
        profile = getattr(route, "profile", None)
        if profile:
            return str(profile)
    return "default"
