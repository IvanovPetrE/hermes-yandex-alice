"""Хранилище состояния беседы (docs/PLAN.md, этап 4) — настоящий SQLite во временном каталоге, без Hermes."""
import dataclasses
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "yandex-alice"))
import dialog as d  # noqa: E402
import storage  # noqa: E402

# Каждое поле — не по умолчанию: потеря любого при записи видна в сравнении.
FULL = d.State(
    conversation="c1",
    question=d.Question(asked_at=100.5, approval=True, to_telegram=True, restarted_at=200.0, internal_seen=True),
    answer=d.Answer(parts=("Первая часть.", "Вторая часть."), telegram="Алиса: подробно", whole="Алиса: всё",
                    ready_at=150.0, undelivered=True, offered=True),
    holding="wait", prompt=d.CONTINUE, wait_round=2, held="удержанная фраза", orphans=("c0",),
    last_owner_at=90.0, reading=("Часть 1.", "Часть 2."), reading_pos=1, last_said="Продолжить?",
    announce="объявление", notice="предупреждение", asks=(80.0, 85.5), last_turn="s1-3", last_reply="Продолжить?",
    answered=True,
)


def _good() -> dict:
    return json.loads(storage.encode(FULL))


def _bad_records() -> dict:
    other_version, extra, missing, bad_parts, bad_type = _good(), _good(), _good(), _good(), _good()
    other_version["v"] = 2
    extra["state"]["unknown"] = 1
    del missing["state"]["last_reply"]
    bad_parts["state"]["answer"]["parts"] = [1, 2]
    bad_type["state"]["wait_round"] = "2"
    return {"not json": "{", "not an object": "[]", "other version": json.dumps(other_version),
            "extra field": json.dumps(extra), "missing field": json.dumps(missing),
            "answer parts not texts": json.dumps(bad_parts), "int as text": json.dumps(bad_type)}


class DecodeTests(unittest.TestCase):
    def test_bad_records_are_discarded(self) -> None:
        for name, text in _bad_records().items():
            with self.subTest(name):
                self.assertIsNone(storage.decode(text))


class SchemaTests(unittest.TestCase):
    def test_schema_lists_every_field_of_the_dialog_state(self) -> None:
        for cls, schema in ((d.State, storage.STATE_FIELDS), (d.Question, storage.QUESTION_FIELDS),
                            (d.Answer, storage.ANSWER_FIELDS)):
            with self.subTest(cls.__name__):
                self.assertEqual(list(schema), [f.name for f in dataclasses.fields(cls)])


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)   # после закрытия соединений: очистки идут в обратном порядке
        self.path = Path(tmp.name) / "state.db"

    def open(self) -> "storage.Store":
        store = storage.Store(sqlite3.connect(self.path))
        self.addCleanup(store.close)
        return store

    def test_deleted_texts_are_overwritten_in_the_file(self) -> None:
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        storage.Store(conn)
        self.assertEqual(conn.execute("PRAGMA secure_delete").fetchone()[0], 1)

    def test_writes_do_not_wait_for_fsync(self) -> None:
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        storage.Store(conn)
        self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 1)   # NORMAL (решение владельца)

    def test_empty_database_has_no_state(self) -> None:
        self.assertIsNone(self.open().load())

    def test_saved_state_comes_back_after_restart(self) -> None:
        self.open().save(FULL)
        self.assertEqual(self.open().load(), FULL)

    def test_later_save_replaces_the_earlier_one(self) -> None:
        store = self.open()
        store.save(FULL)
        store.save(d.State(conversation="c2"))
        self.assertEqual(self.open().load(), d.State(conversation="c2"))


if __name__ == "__main__":
    unittest.main()
