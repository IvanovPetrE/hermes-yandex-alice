"""Хранилище состояния беседы (docs/DESIGN.md, этап 4) — настоящий SQLite во временном каталоге, без Hermes."""
import dataclasses
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "yandex-alice"))
import delivery  # noqa: E402
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


class ReadingSnapshotTests(unittest.TestCase):
    def test_older_snapshot_after_page_two_sends_all_available_parts(self):
        old = _good()
        old["state"].pop("reading_whole", None)
        old["state"].update(reading=["Раз.", "Два.", "Три."], reading_pos=1, announce="", notice="")
        st = storage.decode(json.dumps(old))
        event = d.Utterance(now=100, turn_id="no", text="нет", intents=("YANDEX.REJECT",))
        _, acts = d.step(st, event)
        self.assertEqual(acts, (d.Telegram("c1", "Алиса:\nРаз. Два. Три.", ("Раз.", "Два.", "Три.")),
                                d.Say("Ответ пришлю в Телеграм.")))

    def test_full_text_round_trip_and_retention(self):
        base = d.State(conversation="c1", prompt=d.CONTINUE, reading=("Раз.", "Два."),
                       reading_whole="Алиса:\n**Раз. Два.**", last_owner_at=100)
        self.assertEqual(storage.decode(storage.encode(base)), base)
        for event in (d.Tick(now=100 + 86400),
                      d.Utterance(now=101, turn_id="s1-1", text="", launch=True),
                      d.Utterance(now=101, turn_id="s1-1", text="да", intents=("YANDEX.CONFIRM",)),
                      d.Utterance(now=101, turn_id="s1-1", text="новая тема"),
                      d.Utterance(now=101, turn_id="s1-1", text="новый диалог", intents=("hya.dialog.new",), fresh_id="c2")):
            with self.subTest(event=event):
                st, _ = d.step(base, event)
                snapshot = json.loads(storage.encode(st))["state"]
                self.assertNotIn("reading_whole", snapshot)
                self.assertNotIn("**Раз.", str(snapshot))

    def test_explicit_null_full_text_is_invalid(self):
        bad = _good()
        bad["state"]["reading_whole"] = None
        self.assertIsNone(storage.decode(json.dumps(bad)))


class AliveAtTests(unittest.TestCase):
    """HERMESYAND-3: признак жизни хода — в снимке; снимок прежней выкладки (без поля) читается, а не обнуляется."""

    def test_alive_at_survives_a_round_trip(self) -> None:
        state = dataclasses.replace(FULL, question=dataclasses.replace(FULL.question, alive_at=180.25))
        self.assertEqual(storage.decode(storage.encode(state)), state)

    def test_a_snapshot_without_alive_at_is_read_with_none(self) -> None:
        old = _good()
        del old["state"]["question"]["alive_at"]
        self.assertEqual(storage.decode(json.dumps(old)), FULL)

    def test_alive_at_of_a_wrong_type_discards_the_snapshot(self) -> None:
        bad = _good()
        bad["state"]["question"]["alive_at"] = "180"
        self.assertIsNone(storage.decode(json.dumps(bad)))


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


class OutboxTests(unittest.TestCase):
    """Отправки в Telegram до исхода (этап 5): переживают перезапуск, исход удаляет текст."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "state.db"

    def open(self) -> "storage.Store":
        store = storage.Store(sqlite3.connect(self.path))
        self.addCleanup(store.close)
        return store

    def test_a_send_survives_restart_with_its_attempts(self) -> None:
        store = self.open()
        sid = store.add("c1", "Алиса:\nподробно", ("Часть.",), 100.0)
        store.started(sid)
        self.assertEqual(self.open().pending(),
                         [delivery.Pending(id=sid, conversation="c1", text="Алиса:\nподробно", fallback=("Часть.",),
                                           created_at=100.0, attempts=1)])

    def test_sends_come_back_in_order_and_done_removes_one(self) -> None:
        store = self.open()
        first, second = store.add("c1", "раз", (), 1.0), store.add("c1", "два", (), 2.0)
        store.done(first)
        self.assertEqual([r.text for r in self.open().pending()], ["два"])
        self.assertLess(first, second)

    def test_a_broken_row_is_dropped(self) -> None:
        store = self.open()
        sid = store.add("c1", "раз", (), 1.0)
        conn = sqlite3.connect(self.path)
        with conn:
            conn.execute("UPDATE outbox SET fallback = '[1' WHERE id = ?", (sid,))
        conn.close()
        self.assertEqual(self.open().pending(), [])
        self.assertEqual(self.open().pending(), [])   # удалена, а не пропущена один раз

    def test_the_state_snapshot_is_untouched_by_sends(self) -> None:
        store = self.open()
        store.save(FULL)
        store.add("c1", "раз", (), 1.0)
        self.assertEqual(self.open().load(), FULL)


if __name__ == "__main__":
    unittest.main()
