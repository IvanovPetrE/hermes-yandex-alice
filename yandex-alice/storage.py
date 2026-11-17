"""Хранилище состояния беседы (docs/PLAN.md, этап 4) — только стандартная библиотека, без Hermes.

Одна строка SQLite — снимок `dialog.State` в JSON после каждого шага машины: незабранный ответ, вопрос в работе, исход
доставки (`Answer.undelivered`, `notice`) и дедупликация ретрая (`last_turn`/`last_reply`) — это поля самого состояния,
второй модели того же нет. Соединение передаёт адаптер (`plugins.plugin_storage.plugin_db`), тесты — свой SQLite.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

try:
    from . import dialog as d
except ImportError:  # тесты импортируют модули плагина как модули верхнего уровня
    import dialog as d  # type: ignore[no-redef]

VERSION = 1
BUSY_MS = 200   # пишет только этот адаптер; 5 с из plugin_db больше бюджета ответа Алисе (~3,5 с)
_SCHEMA = "CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)"

# Типы полей — явно, а не из аннотаций: формат записи меняется только вместе с VERSION.
STR, FLOAT, INT, BOOL, TEXTS, FLOATS = "str", "float", "int", "bool", "texts", "floats"
OPT_FLOAT = "float?"
QUESTION_FIELDS = {"asked_at": FLOAT, "approval": BOOL, "to_telegram": BOOL, "restarted_at": OPT_FLOAT,
                   "internal_seen": BOOL}
ANSWER_FIELDS = {"parts": TEXTS, "telegram": STR, "whole": STR, "ready_at": FLOAT, "undelivered": BOOL,
                 "offered": BOOL}
STATE_FIELDS = {"conversation": STR, "question": QUESTION_FIELDS, "answer": ANSWER_FIELDS, "holding": STR,
                "prompt": STR, "wait_round": INT, "held": STR, "orphans": TEXTS, "last_owner_at": OPT_FLOAT,
                "reading": TEXTS, "reading_pos": INT, "last_said": STR, "announce": STR, "notice": STR,
                "asks": FLOATS, "last_turn": STR, "last_reply": STR, "answered": BOOL}


def encode(state: d.State) -> str:
    return json.dumps({"v": VERSION, "state": asdict(state)}, ensure_ascii=False)


class _Bad(Exception):
    pass


def _number(raw) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise _Bad
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
    raise _Bad


def _record(cls, fields: dict, raw):
    if not isinstance(raw, dict) or set(raw) != set(fields):
        raise _Bad
    values = {}
    for name, kind in fields.items():
        item = raw[name]
        if isinstance(kind, dict):
            values[name] = None if item is None else _record(d.Question if kind is QUESTION_FIELDS else d.Answer,
                                                             kind, item)
        else:
            values[name] = _value(kind, item)
    return cls(**values)


def decode(text: str) -> d.State | None:
    """Снимок или None: чужая версия, лишнее или недостающее поле, неверный тип — без исключений (начать с чистого)."""
    try:
        raw = json.loads(text)
        if not isinstance(raw, dict) or set(raw) != {"v", "state"} or raw["v"] != VERSION:
            return None
        return _record(d.State, STATE_FIELDS, raw["state"])
    except (_Bad, ValueError, TypeError):
        return None


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        conn.execute("PRAGMA secure_delete=ON")   # тексты — персональные данные: удалённое затирается в файле
        conn.execute(f"PRAGMA busy_timeout={BUSY_MS}")
        conn.execute("PRAGMA synchronous=NORMAL")   # без fsync на каждом шаге: падение процесса записи не теряет
        with conn:
            conn.execute(_SCHEMA)

    def load(self) -> d.State | None:
        row = self._conn.execute("SELECT data FROM state WHERE id = 1").fetchone()
        return None if row is None else decode(row[0])

    def save(self, state: d.State) -> None:
        with self._conn:
            self._conn.execute("INSERT OR REPLACE INTO state (id, data) VALUES (1, ?)", (encode(state),))

    def close(self) -> None:
        self._conn.close()
