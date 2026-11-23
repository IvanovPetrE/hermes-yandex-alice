"""Хранилище состояния беседы (docs/DESIGN.md, этап 4) — только стандартная библиотека, без Hermes.

Одна строка SQLite — снимок `dialog.State` в JSON после каждого шага машины: незабранный ответ, вопрос в работе, исход
доставки (`Answer.undelivered`, `notice`) и дедупликация ретрая (`last_turn`/`last_reply`) — это поля самого состояния,
второй модели того же нет. Отдельная таблица `outbox` (этап 5) — очередь отправок в Telegram до исхода: текст, части для
«Озвучить?», число начатых попыток; строка удаляется с исходом (`delivery.Courier`). Соединение передаёт адаптер
(`plugins.plugin_storage.plugin_db`), тесты — свой SQLite.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict

try:
    from . import delivery
    from . import dialog as d
except ImportError:  # тесты импортируют модули плагина как модули верхнего уровня
    import delivery  # type: ignore[no-redef]
    import dialog as d  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

VERSION = 1
BUSY_MS = 200   # пишет только этот адаптер; 5 с из plugin_db больше бюджета ответа Алисе (~3,5 с)
_SCHEMA = "CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)"
# Отправки в Telegram до исхода (этап 5): строка удаляется с исходом; снимок `state` и его VERSION не затрагиваются.
_OUTBOX = ("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation TEXT NOT NULL, "
           "text TEXT NOT NULL, fallback TEXT NOT NULL, created_at REAL NOT NULL, attempts INTEGER NOT NULL)")
# conversation беседы Hermes — отдельно от снимка и его VERSION (HERMESYAND-28): переживает нечитаемый снимок.
_META = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"

# Типы полей — явно, а не из аннотаций: формат записи меняется только вместе с VERSION.
STR, FLOAT, INT, BOOL, TEXTS, FLOATS = "str", "float", "int", "bool", "texts", "floats"
OPT_FLOAT = "float?"
QUESTION_FIELDS = {"asked_at": FLOAT, "approval": BOOL, "to_telegram": BOOL, "restarted_at": OPT_FLOAT,
                   "internal_seen": BOOL, "alive_at": OPT_FLOAT}
# Поля, добавленные без смены VERSION: старые снимки получают значения по умолчанию из dataclass.
_ADDED = frozenset({"alive_at", "lost_late", "reading_whole"})
ANSWER_FIELDS = {"parts": TEXTS, "telegram": STR, "whole": STR, "ready_at": FLOAT, "undelivered": BOOL,
                 "offered": BOOL}
STATE_FIELDS = {"conversation": STR, "question": QUESTION_FIELDS, "answer": ANSWER_FIELDS, "holding": STR,
                "prompt": STR, "wait_round": INT, "held": STR, "orphans": TEXTS, "last_owner_at": OPT_FLOAT,
                "reading": TEXTS, "reading_pos": INT, "last_said": STR, "announce": STR, "notice": STR,
                "asks": FLOATS, "last_turn": STR, "last_reply": STR, "answered": BOOL,
                "lost_late": BOOL, "reading_whole": STR}


def encode(state: d.State) -> str:
    fields = asdict(state)
    if not state.reading_whole:
        del fields["reading_whole"]
    return json.dumps({"v": VERSION, "state": fields}, ensure_ascii=False)


class _Bad(Exception):
    """Запись не читается; `reason` — только класс причины (в лог), без данных."""

    def __init__(self, reason: str = "bad_type"):
        super().__init__(reason)
        self.reason = reason


def _number(raw) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise _Bad("bad_type")
    return float(raw)


def _value(kind, raw):
    if kind == STR and isinstance(raw, str):
        return raw
    if kind == INT and isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if kind == BOOL and isinstance(raw, bool):
        return raw
    if kind == FLOAT:
        return _number(raw)
    if kind == OPT_FLOAT:
        return None if raw is None else _number(raw)
    if kind == TEXTS and isinstance(raw, list) and all(isinstance(x, str) for x in raw):
        return tuple(raw)
    if kind == FLOATS and isinstance(raw, list):
        return tuple(_number(x) for x in raw)
    raise _Bad("bad_type")


def _record(cls, fields: dict, raw):
    if not isinstance(raw, dict):
        raise _Bad("bad_type")
    if not set(fields) - _ADDED <= set(raw):
        raise _Bad("missing_field")
    if not set(raw) <= set(fields):
        raise _Bad("extra_field")
    values = {}
    for name, kind in fields.items():
        if name in _ADDED and name not in raw:
            continue  # значение по умолчанию задаёт dataclass
        item = raw[name]
        if isinstance(kind, dict):
            values[name] = None if item is None else _record(d.Question if kind is QUESTION_FIELDS else d.Answer,
                                                             kind, item)
        else:
            values[name] = _value(kind, item)
    return cls(**values)


def _read(text: str) -> tuple[d.State | None, str]:
    """`(снимок, "")` или `(None, класс причины)`: invalid_json, not_object, bad_shape, other_version,
    missing_field, extra_field, bad_type."""
    try:
        raw = json.loads(text)
        if not isinstance(raw, dict):
            return None, "not_object"
        if set(raw) != {"v", "state"}:
            return None, "bad_shape"
        if raw["v"] != VERSION:
            return None, "other_version"
        return _record(d.State, STATE_FIELDS, raw["state"]), ""
    except _Bad as bad:
        return None, bad.reason
    except ValueError:
        return None, "invalid_json"
    except TypeError:
        return None, "bad_type"


def decode(text: str) -> d.State | None:
    """Снимок или None: чужая версия, лишнее или недостающее поле, неверный тип — без исключений (начать с чистого)."""
    return _read(text)[0]


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        conn.execute("PRAGMA secure_delete=ON")   # тексты — персональные данные: удалённое затирается в файле
        conn.execute(f"PRAGMA busy_timeout={BUSY_MS}")
        conn.execute("PRAGMA synchronous=NORMAL")   # без fsync на каждом шаге: падение процесса записи не теряет
        with conn:
            conn.execute(_SCHEMA)
            conn.execute(_OUTBOX)
            conn.execute(_META)

    def load(self) -> d.State | None:
        """Снимок; нечитаемый (чужая версия, лишнее поле…) — чистое состояние с прежним `conversation` из `meta`
        (HERMESYAND-28), а без него — `None`. Читаемый снимок главнее `meta`."""
        row = self._conn.execute("SELECT data FROM state WHERE id = 1").fetchone()
        if row is None:
            return None
        state, reason = _read(row[0])
        if state is not None:
            return state
        logger.warning("state snapshot unreadable (%s): starting clean", reason)   # только класс: в снимке тексты беседы
        kept = self._conn.execute("SELECT value FROM meta WHERE key = 'conversation'").fetchone()
        return d.State(conversation=kept[0]) if kept and kept[0] else None

    def save(self, state: d.State) -> None:
        with self._conn:   # снимок и conversation — одной транзакцией
            self._conn.execute("INSERT OR REPLACE INTO state (id, data) VALUES (1, ?)", (encode(state),))
            self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('conversation', ?)",
                               (state.conversation,))

    # --- отправки в Telegram до исхода (этап 5) ---------------------------------------------------------------------

    def add(self, conversation: str, text: str, fallback: tuple[str, ...], created_at: float) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO outbox (conversation, text, fallback, created_at, attempts) VALUES (?, ?, ?, ?, 0)",
                (conversation, text, json.dumps(list(fallback), ensure_ascii=False), created_at))
        return int(cur.lastrowid)

    def started(self, send_id: int) -> None:
        """Попытка началась — до вызова отправителя: обрыв после этого считается оборванной попыткой."""
        with self._conn:
            self._conn.execute("UPDATE outbox SET attempts = attempts + 1 WHERE id = ?", (send_id,))

    def done(self, send_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM outbox WHERE id = ?", (send_id,))

    def pending(self) -> list[delivery.Pending]:
        """Отправки без исхода в порядке постановки; кривая строка удаляется (начать без неё)."""
        rows = self._conn.execute(
            "SELECT id, conversation, text, fallback, created_at, attempts FROM outbox ORDER BY id").fetchall()
        out, bad = [], []
        for sid, conversation, text, fallback, created_at, attempts in rows:
            try:
                parts = json.loads(fallback)
                if not isinstance(parts, list) or not all(isinstance(x, str) for x in parts):
                    raise _Bad
                out.append(delivery.Pending(id=sid, conversation=conversation, text=text, fallback=tuple(parts),
                                            created_at=_number(created_at), attempts=int(attempts)))
            except (_Bad, ValueError, TypeError):
                bad.append(sid)
        if bad:
            with self._conn:
                self._conn.executemany("DELETE FROM outbox WHERE id = ?", [(sid,) for sid in bad])
        return out

    def close(self) -> None:
        self._conn.close()
