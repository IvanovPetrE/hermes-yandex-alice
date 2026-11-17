"""Задачи cron, созданные голосом, не должны уходить в голосовой канал — без зависимостей от Hermes.

Перенесено из пробы S6 (`prototypes/s6/plugin/cron_guard.py`). Задача cron, созданная в голосовой сессии, по
умолчанию получает `deliver=origin` — то есть голосовую беседу, а голосовой канал не может заговорить первым:
результат потерялся бы. `guard_args` — то, что хук `pre_tool_call` добавляет к аргументам `cronjob_manage`:
заменяются только «алисины» адреса на `telegram:<чат>`, всё, что агент выбрал явно, остаётся.
"""

PLATFORM = "yandex_alice"
TOOL = "cronjob_manage"


def _blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _is_alice(token):
    t = token.strip().lower()
    return t == "origin" or t == PLATFORM or t.startswith(PLATFORM + ":")


def _tokens(value):
    """Токены через запятую из `str` или списка `str` (Hermes принимает оба вида); `None` — если ни то ни другое."""
    if isinstance(value, str):
        parts = [value]
    elif isinstance(value, list) and all(isinstance(p, str) for p in value):
        parts = value
    else:
        return None
    return [t.strip() for p in parts for t in p.split(",") if t.strip()]


def _rewrite(value, target):
    """Значение с заменёнными адресами Алисы (без повторов, порядок сохранён) или `None`, если ничего не менялось."""
    tokens = _tokens(value)
    if not tokens or not any(_is_alice(t) for t in tokens):
        return None
    out = []
    for t in tokens:
        t = target if _is_alice(t) else t
        if t not in out:
            out.append(t)
    return ",".join(out)


def valid_chat(chat):
    """Чат Telegram без пробелов, запятых и `:` или `None`. Такое значение нельзя подставить в `telegram:<чат>`
    (топики «чат:тема» не поддерживаются); `validate_config` адаптера отвергает его при старте."""
    text = chat.strip() if isinstance(chat, str) else ""
    if not text or any(c.isspace() or c in ",:" for c in text):
        return None
    return text


def guard_args(args, chat):
    """Аргументы, которые нужно добавить к вызову `cronjob_manage`, или `None` — не трогать вызов.

    `create`: пустой `deliver` (Hermes взял бы origin, то есть голосовой чат) становится целью.
    `create`/`update`: явные адреса Алисы в `deliver` и `failure_deliver` становятся целью.
    Не указанный `deliver` при `update` значит «оставить», не указанный `failure_deliver` — «как `deliver`»:
    оба остаются. Остальные действия не трогаются. Никогда не бросает.
    """
    try:
        return _guard(args, chat)
    except Exception:  # noqa: BLE001 - бросающий хук заблокировал бы все инструменты профиля
        return None


def _guard(args, chat):
    if not isinstance(args, dict):
        return None
    action = args.get("action")
    action = action.strip().lower() if isinstance(action, str) else ""  # как в tools/cronjob_tools.py
    chat = valid_chat(chat)
    if action not in ("create", "update") or chat is None:
        return None
    target = "telegram:" + chat
    out = {}
    deliver = args.get("deliver")
    if action == "create" and _blank(deliver):
        out["deliver"] = target
    else:
        new = _rewrite(deliver, target)
        if new is not None:
            out["deliver"] = new
    new = _rewrite(args.get("failure_deliver"), target)
    if new is not None:
        out["failure_deliver"] = new
    return out or None
