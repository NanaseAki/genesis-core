"""Synthetic regressions for M2-A transaction ownership and value contracts."""

from contextlib import closing
import inspect
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from genesis.core import GenesisCore, Turn, TurnGenerationError, UserInput
from genesis.growth import GrowthCandidate
from genesis.memory import EvidenceOrigin, EvidenceReference, MemoryKind, MemoryRecord
from genesis.models.contracts import Message, ModelError, ModelReply, Role
from genesis.models.fake import FakeModel
from genesis.storage.sqlite import DataIntegrityError, SQLiteStore, TransactionNestingError
from genesis.text import TextContractError


def uid(n: int) -> UUID:
    return UUID(int=n)


class TransactionOwnershipTests(unittest.TestCase):
    def test_interrupt_immediately_after_begin_rolls_back_and_both_connections_can_write(self) -> None:
        for exception_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(exception=exception_type), tempfile.TemporaryDirectory() as root:
                with SQLiteStore(root) as store:
                    function = SQLiteStore._transaction.__wrapped__
                    lines, start = inspect.getsourcelines(function)
                    begin = next(i for i, line in enumerate(lines) if 'connection.execute("BEGIN' in line)
                    # The first executable line after execute returns. This fails
                    # against the original BEGIN-outside-try implementation.
                    target = start + begin + 1
                    def interrupt(frame, event, arg):
                        if frame.f_code is function.__code__ and event == "line" and frame.f_lineno == target:
                            raise exception_type("synthetic immediately after BEGIN")
                        return interrupt
                    previous = sys.gettrace()
                    try:
                        sys.settrace(interrupt)
                        with self.assertRaises(exception_type):
                            store.create_session(uid(2))
                    finally:
                        sys.settrace(previous)
                    self.assertFalse(store._connection.in_transaction)
                    self.assertEqual(store.session_ids(), ())
                    store.create_session(uid(3))
                    with SQLiteStore(root) as other:
                        other.create_session(uid(4))
                    self.assertEqual(store.session_ids(), (uid(3), uid(4)))

    def test_interrupt_raised_by_begin_after_sqlite_accepts_it_is_cleaned_up(self) -> None:
        with tempfile.TemporaryDirectory() as root, SQLiteStore(root) as store:
            connection = store._connection
            class InterruptingConnection:
                armed = True
                def __getattr__(self, name):
                    return getattr(connection, name)
                def execute(self, sql, *args):
                    result = connection.execute(sql, *args)
                    if self.armed and sql.startswith("BEGIN"):
                        self.armed = False
                        raise KeyboardInterrupt("synthetic signal before execute returns")
                    return result
            with patch.object(store, "_connection", InterruptingConnection()):
                with self.assertRaises(KeyboardInterrupt):
                    store.create_session(uid(2))
                self.assertFalse(connection.in_transaction)
                store.create_session(uid(3))

    def test_nested_entry_does_not_roll_back_or_adopt_outer_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as root, SQLiteStore(root) as store:
            with store._transaction() as db:
                db.execute("INSERT INTO sessions(session_id,created_utc) VALUES (?,?)", (str(uid(2)), "synthetic"))
                for write in (False, True):
                    with self.assertRaises(TransactionNestingError), store._transaction(write=write):
                        self.fail("nested body must not execute")
                    self.assertTrue(db.in_transaction)
                    self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
            with SQLiteStore(root) as other:
                self.assertEqual(other.session_ids(), (uid(2),))

    def test_failed_begin_cannot_roll_back_another_connections_work(self) -> None:
        with tempfile.TemporaryDirectory() as root, SQLiteStore(root) as owner, SQLiteStore(root) as contender:
            contender._connection.execute("PRAGMA busy_timeout=0")  # Test-only; production stays at 5 seconds.
            with owner._transaction() as db:
                db.execute("INSERT INTO sessions(session_id,created_utc) VALUES (?,?)", (str(uid(2)), "synthetic"))
                with self.assertRaises(sqlite3.OperationalError):
                    contender.create_session(uid(3))
                self.assertTrue(db.in_transaction)
                self.assertFalse(contender._connection.in_transaction)
            self.assertEqual(contender.session_ids(), (uid(2),))

    def test_interrupt_after_commit_never_invokes_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as root, SQLiteStore(root) as store:
            connection = store._connection
            class ObservedConnection:
                rollbacks = 0
                def __getattr__(self, name):
                    return getattr(connection, name)
                def rollback(self):
                    self.rollbacks += 1
                    connection.rollback()
            observed = ObservedConnection()
            original = store._commit
            def commit_then_interrupt():
                original()
                raise KeyboardInterrupt("synthetic return lost")
            with patch.object(store, "_connection", observed), patch.object(store, "_commit", side_effect=commit_then_interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    store.create_session(uid(2))
            self.assertEqual(observed.rollbacks, 0)
            self.assertFalse(store._connection.in_transaction)
            with SQLiteStore(root) as other:
                self.assertEqual(other.session_ids(), (uid(2),))
            store.create_session(uid(3))


class ValueContractTests(unittest.TestCase):
    def test_invalid_model_ids_are_rejected_in_both_core_paths(self) -> None:
        for invalid in (7, True, False, None, 7.5, b"fake", "", " \t\r\n", "bad\x00id", "bad\ud800"):
            for persistent in (False, True):
                with self.subTest(value=repr(invalid), persistent=persistent), tempfile.TemporaryDirectory() as root:
                    with SQLiteStore(root) as store:
                        class BadID(FakeModel):
                            @property
                            def model_id(self):
                                return invalid
                        model = BadID()
                        core = GenesisCore(store.identity, model, store=store if persistent else None)
                        core.create_session(uid(2))
                        with self.assertRaises(TurnGenerationError) as error:
                            core.send(uid(2), UserInput(uid(3), "synthetic request"))
                        self.assertIsInstance(error.exception.__cause__, ModelError)
                        self.assertEqual(core.get_session(uid(2)).turns, ())
                        self.assertEqual(model.requests, ())
                        if persistent:
                            self.assertEqual(store.attempts(uid(2), uid(3))[0].state, "failed")

    def test_store_rejects_invalid_model_ids_before_any_successful_write(self) -> None:
        with tempfile.TemporaryDirectory() as root, SQLiteStore(root) as store:
            store.create_session(uid(2))
            user_input = UserInput(uid(3), "synthetic request")
            attempt = store.begin_attempt(uid(2), user_input)
            for invalid in (7, True, None, "", " \n", "bad\x00id"):
                with self.subTest(value=repr(invalid)):
                    with self.assertRaises(ModelError):
                        store.commit_turn(attempt, Turn(user_input, Message(Role.ASSISTANT, "synthetic"), invalid))
                    self.assertEqual(store.get_session(uid(2)).turns, ())
                    self.assertEqual(len(store.events(uid(2))), 1)
                    self.assertEqual(store.attempts(uid(2), uid(3))[0].state, "running")
            self.assertEqual(store._connection.execute("SELECT revision FROM sessions").fetchone()[0], 0)

    def test_unicode_model_id_and_reply_are_equal_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with SQLiteStore(root) as store:
                class UnicodeID(FakeModel):
                    @property
                    def model_id(self):
                        return "  合成模型-🌱-e\u0301  "
                model = UnicodeID(["  合成回复\n第二行 🌱\r\n  "])
                core = GenesisCore(store.identity, model, store=store)
                core.create_session(uid(2))
                user_input = UserInput(uid(3), "  合成输入\n第二行 🌱  ")
                original = core.send(uid(2), user_input)
            with SQLiteStore(root) as store:
                core = GenesisCore(store.identity, FakeModel([]), store=store)
                self.assertEqual(core.send(uid(2), user_input), original)
                self.assertEqual(original.model_id, "  合成模型-🌱-e\u0301  ")

    def test_text_values_reject_nul_at_any_position_and_lone_surrogates(self) -> None:
        source = EvidenceReference(uid(1), EvidenceOrigin.USER_INPUT, uid(1))
        constructors = (
            lambda text: UserInput(uid(3), text),
            lambda text: Message(Role.USER, text),
            ModelReply,
            lambda text: MemoryRecord(uid(4), MemoryKind.OBSERVATION, text, (source,)),
            lambda text: GrowthCandidate(uid(5), text, (source,)),
        )
        for text in ("\x00synthetic", "synthetic\x00text", "synthetic\x00", "bad\ud800", "bad\udfff"):
            for constructor in constructors:
                with self.subTest(value=repr(text), constructor=constructor):
                    with self.assertRaises(TextContractError):
                        constructor(text)

    def test_invalid_reply_is_a_contract_failure_with_no_successful_turn(self) -> None:
        for persistent in (False, True):
            with self.subTest(persistent=persistent), tempfile.TemporaryDirectory() as root:
                with SQLiteStore(root) as store:
                    core = GenesisCore(store.identity, FakeModel(["\0synthetic reply"]),
                                       store=store if persistent else None)
                    core.create_session(uid(2))
                    with self.assertRaises(TurnGenerationError) as error:
                        core.send(uid(2), UserInput(uid(3), "synthetic request"))
                    self.assertIsInstance(error.exception.__cause__, TextContractError)
                    self.assertEqual(core.get_session(uid(2)).turns, ())
                    if persistent:
                        self.assertEqual(store.attempts(uid(2), uid(3))[0].error_code, "model_error")

    def test_legal_unicode_newlines_and_controls_survive_sql_and_replay_unchanged(self) -> None:
        # Boundary code points, combining marks and non-NUL controls remain
        # lossless. SQLite must not reject anything the public validator accepts.
        texts = ("  合成中文\n第二行\r\n  ", "\t🌱e\u0301\t", "\x01\x1f\x7f", "\u0080\u07ff\u0800",
                 "\ud7ff\ue000\uffff\U00010000\U0010ffff", "\u200b", "\ufeffsynthetic")
        with tempfile.TemporaryDirectory() as root:
            with SQLiteStore(root) as store:
                core = GenesisCore(store.identity, FakeModel(texts), store=store)
                core.create_session(uid(2))
                original = []
                for n, text in enumerate(texts, 10):
                    original.append(core.send(uid(2), UserInput(uid(n), text)))
            with SQLiteStore(root) as store:
                core = GenesisCore(store.identity, FakeModel([]), store=store)
                for turn, text in zip(original, texts):
                    self.assertEqual(core.send(uid(2), turn.user_input), turn)
                    self.assertEqual(turn.user_input.content, text)
                    self.assertEqual(turn.assistant.content, text)

    def test_event_and_derivation_boundaries_reject_implicit_text_coercion(self) -> None:
        with tempfile.TemporaryDirectory() as root, SQLiteStore(root) as store:
            core = GenesisCore(store.identity, FakeModel(), store=store)
            core.create_session(uid(2)); core.send(uid(2), UserInput(uid(3), "synthetic"))
            source = store.events(uid(2))[0].reference.event_id
            for bad in (7, True, None, b"synthetic", "bad\0text", "bad\ud800"):
                with self.subTest(value=repr(bad)):
                    with self.assertRaises(TextContractError):
                        store.record_derived(uid(2), source, kind="summary", content=bad)
                    with self.assertRaises(TextContractError), store._transaction() as db:
                        store._event(db, uid(90), uid(2), "user_input", "user_input", uid(90), None, bad)
            self.assertEqual(len(store.events(uid(2))), 2)

    def test_existing_v1_database_is_preserved_and_bad_legacy_text_is_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with SQLiteStore(root) as store:
                core = GenesisCore(store.identity, FakeModel(), store=store)
                core.create_session(uid(2)); core.send(uid(2), UserInput(uid(3), "synthetic"))
                identity, day0, path = store.identity, store.day0(), store.path
            original = path.read_bytes()
            with SQLiteStore(root) as store:
                self.assertEqual(store.identity, identity)
                self.assertEqual(store.day0(), day0)
                self.assertEqual(store._connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(path.read_bytes(), original)
            # Simulate old v1 data that its SQL constraint allowed: no auto repair.
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE events SET content=? WHERE kind='model_reply'", ("synthetic\0legacy",))
            invalid_bytes = path.read_bytes()
            with self.assertRaises(DataIntegrityError):
                SQLiteStore(root)
            self.assertEqual(path.read_bytes(), invalid_bytes)
