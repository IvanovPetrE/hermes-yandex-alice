"""Pure layer of the S6 probe (stdlib only, no Hermes): keep cron jobs created by voice off the voice channel.

Model of what stage 3 will own. A cron job created in a voice session gets ``deliver=origin`` by default,
i.e. the voice conversation - and the voice channel can never speak first, so such a result would be lost.
``guard_args`` is what a ``pre_tool_call`` hook merges into ``cronjob_manage`` arguments: only "Alice"
addresses are replaced with ``telegram:<chat>``; whatever else the agent chose explicitly stays.
"""

PLATFORM = "yandex_alice"
TOOL = "cronjob_manage"


def _blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _is_alice(token):
    t = token.strip().lower()
    return t == "origin" or t == PLATFORM or t.startswith(PLATFORM + ":")


def _tokens(value):
    """Comma-separated tokens of a ``str`` or a ``list`` of ``str`` (Hermes accepts both); ``None`` if neither."""
    if isinstance(value, str):
        parts = [value]
    elif isinstance(value, list) and all(isinstance(p, str) for p in value):
        parts = value
    else:
        return None
    return [t.strip() for p in parts for t in p.split(",") if t.strip()]


def _rewrite(value, target):
    """The value with Alice tokens replaced (deduplicated, order kept), or ``None`` when nothing changed."""
    tokens = _tokens(value)
    if not tokens or not any(_is_alice(t) for t in tokens):
        return None
    out = []
    for t in tokens:
        t = target if _is_alice(t) else t
        if t not in out:
            out.append(t)
    return ",".join(out)


def guard_args(args, chat):
    """Arguments to merge into a ``cronjob_manage`` call, or ``None`` to leave it untouched.

    ``create``: an empty ``deliver`` (Hermes would use the origin, the voice chat) becomes the target.
    ``create``/``update``: explicit Alice tokens of ``deliver`` and ``failure_deliver`` become the target.
    An omitted ``deliver`` on ``update`` means "keep" and an omitted ``failure_deliver`` means "follow
    deliver" - both stay. Every other action is untouched.
    """
    try:
        return _guard(args, chat)
    except Exception:  # noqa: BLE001 - a raising hook would block every tool of the profile
        return None


def _valid_chat(chat):
    text = chat.strip() if isinstance(chat, str) else ""
    if not text or any(c.isspace() or c in ",:" for c in text):
        return None
    return text


def _guard(args, chat):
    if not isinstance(args, dict):
        return None
    action = args.get("action")
    action = action.strip().lower() if isinstance(action, str) else ""  # as tools/cronjob_tools.py does
    chat = _valid_chat(chat)
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
