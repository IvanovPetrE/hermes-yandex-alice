"""S7 probe: reading the last answer of a voice conversation from Hermes' history (``state.db`` of the target profile).

The pure parts (``last_answer``, ``read_tail``, ``pick_profile``) need no Hermes and are covered by ``HistoryTests``
of the probe. ``read_last_answer`` is the chain the stage-3 adapter would run in a worker thread:

    routes of ``load_gateway_config()`` -> profile -> home (``hermes_constants``) -> ``SessionDB(read_only=True)``
    -> ``find_session_by_origin`` -> ``resolve_resume_session_id`` -> ``get_messages`` (tail, in portions).

Imports of Hermes modules are lazy and limited to ``public-api.md`` (``gateway.config``, ``hermes_constants``,
``hermes_state``). It never reads ``os.environ`` and never touches ``sessions.json`` / ``gateway_routing``.

One read at a time: a worker that cannot take ``_LOCK`` at once returns ``busy`` (an error outcome) instead of
queueing another thread behind a stuck one; the ``SessionDB`` is closed in ``finally`` inside the worker.
"""

import threading
import time
from pathlib import Path

TAIL = 20          # messages per portion
MAX_PORTIONS = 5   # portions per read

FOUND, MISSING, ERROR = "found", "missing", "error"
_LOCK = threading.Lock()


def last_answer(rows):
    """The newest agent answer WITH text in ``rows`` (oldest first), or ``None``.

    An answer is an ``assistant`` message with a non-empty string ``content`` and without ``tool_calls`` (a comment
    written next to a tool call is not the answer) and not a compression summary.
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
    """``(text or None, portions read)``: the tail in portions, newest first, until an answer is found."""
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
    """The target profile of ``platform`` from gateway routes: the first enabled route of that platform that has
    no discriminator (no chat / thread / guild / user); ``"default"`` when there is none."""
    for route in routes or []:
        if getattr(route, "platform", None) != platform or not getattr(route, "enabled", True):
            continue
        if any(getattr(route, name, None) for name in ("chat_id", "thread_id", "guild_id", "user_id")):
            continue
        profile = getattr(route, "profile", None)
        if profile:
            return str(profile)
    return "default"


def target_home(profile):
    """The home directory of ``profile``. ``profiles/<name>`` is an ASSUMPTION derived from
    ``hermes_constants.profile_name_for_home`` (no public name -> home function); it is checked, not trusted."""
    from hermes_constants import get_default_hermes_root, profile_name_for_home

    root = Path(get_default_hermes_root())
    if profile in ("", "default"):
        return root
    home = root / "profiles" / profile
    if profile_name_for_home(home) != profile:
        raise LookupError("profile_home")
    return home


def _summary(rows):
    return [dict(role=r.get("role"), tool_calls=bool(r.get("tool_calls")), length=len(str(r.get("content") or "")),
                 active=r.get("active"), summary=bool(r.get("_compressed_summary"))) for r in rows[-6:]]


def read_last_answer(platform, chat_id, *, tail=TAIL, portions=MAX_PORTIONS, force_profile=None, db=None):
    """Run the whole chain; return a dict with ``outcome`` (found / missing / error), ``text``, ``why`` and timings.

    ``db`` (probe only): a warm read-only handle to reuse instead of opening one per read.
    """
    out = dict(outcome=ERROR, text=None, why=None, steps={}, parts=0)
    if not _LOCK.acquire(blocking=False):
        out["why"] = "busy"
        return out
    t0 = time.perf_counter()
    handle, own = db, db is None

    def mark(name):
        out["steps"][name] = round((time.perf_counter() - t0) * 1000, 1)

    try:
        if handle is None:
            from gateway.config import load_gateway_config

            profile = force_profile or pick_profile(load_gateway_config().profile_routes, platform)
            out["profile"] = profile
            home = target_home(profile)
            mark("route")
            path = home / "state.db"
            if not path.exists():
                out["why"] = "no_state_db"
                return out
            from hermes_state import SessionDB

            handle = SessionDB(path, read_only=True)
            mark("open")
        sid = handle.find_session_by_origin(platform=platform, chat_id=str(chat_id))
        mark("find")
        out["session_found"] = sid is not None
        if sid is None:
            out["outcome"] = MISSING
            return out
        tip = handle.resolve_resume_session_id(sid)
        out["tip_is_same"] = tip == sid
        mark("resolve")
        text, parts = read_tail(handle, tip, tail, portions)
        mark("messages")
        out["parts"] = parts
        out["text"] = text
        out["outcome"] = FOUND
        out["tail_rows"] = _summary(handle.get_messages(tip, latest=True, limit=tail))
        return out
    except Exception as exc:  # noqa: BLE001 - every failure is an "error" outcome with the class only
        out["outcome"] = ERROR
        out["why"] = type(exc).__name__
        return out
    finally:
        if own and handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
        _LOCK.release()
