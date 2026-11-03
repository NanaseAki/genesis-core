"""Synthetic SQLite scenarios: transactions, retries and trusted provenance."""

from dataclasses import FrozenInstanceError
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from genesis.core import (
    DuplicateInputError, GenesisCore, PolicyDeniedError, SessionExistsError,
    SessionNotFoundError, TurnGenerationError, UserInput,
)
from genesis.identity import CompanionIdentity
from genesis.memory import EvidenceOrigin, EvidenceReference
from genesis.models.contracts import ModelError
from genesis.models.fake import FakeModel
from genesis.policy import PolicyDecision
from genesis.storage.sqlite import SQLiteStore, StaleAttemptError


def uid(value: int) -> UUID:
    return UUID(int=value)


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = self.open_store()
        self.model = FakeModel()
        self.core = GenesisCore(self.store.identity, self.model, store=self.store)
        self.core.create_session(uid(2))
        self.input = UserInput(uid(3), "synthetic durable input")

    def open_store(self) -> SQLiteStore:
        store = SQLiteStore(Path(self.tmp.name))
        self.addCleanup(store.close)
        return store

    def scalar(self, sql: str):
        return self.store._connection.execute(sql).fetchone()[0]

    def test_initialization_is_once_and_uuid_survives_reopen(self) -> None:
        identity, day0 = self.store.identity, self.store.day0()
        self.assertEqual(self.scalar("SELECT count(*) FROM events WHERE kind='day0'"), 1)
        self.store.close()
        with SQLiteStore(self.tmp.name) as reopened:
            self.assertEqual(reopened.identity, identity)
            self.assertEqual(reopened.day0(), day0)
            self.assertEqual(reopened._connection.execute("SELECT count(*) FROM companion").fetchone()[0], 1)
            self.assertEqual(reopened._connection.execute("SELECT count(*) FROM events WHERE kind='day0'").fetchone()[0], 1)

    def test_committed_replay_after_reopen_does_not_call_new_model(self) -> None:
        original = self.core.send(uid(2), self.input)
        snapshot = self.core.get_session(uid(2))
        self.store.close()
        with SQLiteStore(self.tmp.name) as reopened:
            model = FakeModel([])
            core = GenesisCore(reopened.identity, model, store=reopened)
            self.assertEqual(core.get_session(uid(2)), snapshot)
            self.assertEqual(reopened.session_ids(), (uid(2),))
            self.assertEqual(core.send(uid(2), self.input), original)
            self.assertEqual(model.requests, ())
            self.assertEqual(reopened.attempts(uid(2), uid(3))[0].state, "committed")

    def test_conflict_after_failure_and_after_commit(self) -> None:
        failing = GenesisCore(self.store.identity, FakeModel([ModelError("synthetic")]), store=self.store)
        with self.assertRaises(TurnGenerationError):
            failing.send(uid(2), self.input)
        changed = UserInput(self.input.input_id, "synthetic different content")
        with self.assertRaises(DuplicateInputError):
            self.core.send(uid(2), changed)
        self.core.send(uid(2), self.input)
        with self.assertRaises(DuplicateInputError):
            self.core.send(uid(2), changed)
        self.assertEqual(len(self.model.requests), 1)

    def test_same_text_new_id_is_independent_and_sessions_are_isolated(self) -> None:
        self.core.send(uid(2), self.input)
        self.core.send(uid(2), UserInput(uid(4), self.input.content))
        self.core.create_session(uid(5))
        self.core.send(uid(5), UserInput(uid(3), "synthetic other session"))
        self.assertEqual(len(self.core.get_session(uid(2)).turns), 2)
        self.assertEqual(len(self.core.get_session(uid(5)).turns), 1)
        self.assertEqual(len(self.model.requests[-1].messages), 1)
        events = self.store.events(uid(2))
        roots = [e.reference.event_id for e in events if e.kind == "user_input"]
        self.assertEqual(len(set(roots)), 2)
        self.assertEqual(tuple(e.sequence for e in events), tuple(sorted(e.sequence for e in events)))

    def test_expected_failure_is_sanitized_and_retry_is_explicit(self) -> None:
        model = FakeModel([ModelError("synthetic private diagnostic"), "synthetic recovered"])
        core = GenesisCore(self.store.identity, model, store=self.store)
        with self.assertRaises(TurnGenerationError) as result:
            core.send(uid(2), self.input)
        self.assertIsInstance(result.exception.__cause__, ModelError)
        self.assertEqual(core.get_session(uid(2)).turns, ())
        failed = self.store.attempts(uid(2), uid(3))[0]
        self.assertEqual((failed.state, failed.error_code), ("failed", "model_error"))
        self.assertNotIn("private diagnostic", self.store.path.read_bytes().decode(errors="ignore"))
        self.assertEqual(core.send(uid(2), self.input).assistant.content, "synthetic recovered")
        self.assertEqual([a.state for a in self.store.attempts(uid(2), uid(3))], ["failed", "committed"])
        self.assertEqual(len([e for e in self.store.events(uid(2)) if e.kind == "user_input"]), 1)

    def test_unexpected_error_and_keyboard_interrupt_propagate(self) -> None:
        for exc, state in ((RuntimeError("synthetic unexpected"), "failed"),
                           (KeyboardInterrupt("synthetic interrupted"), "interrupted")):
            with self.subTest(state=state):
                model = FakeModel()
                core = GenesisCore(self.store.identity, model, store=self.store)
                user_input = UserInput(uid(30 if state == "failed" else 31), "synthetic failure")
                with patch.object(model, "generate", side_effect=exc):
                    with self.assertRaises(type(exc)):
                        core.send(uid(2), user_input)
                self.assertEqual(self.store.attempts(uid(2), user_input.input_id)[0].state, state)
                self.assertEqual(core.get_session(uid(2)).turns, ())

    def test_invalid_model_reply_does_not_commit(self) -> None:
        with patch.object(self.model, "generate", return_value="synthetic wrong type"):
            with self.assertRaises(TurnGenerationError):
                self.core.send(uid(2), self.input)
        self.assertEqual(self.core.get_session(uid(2)).turns, ())
        self.assertEqual(self.store.attempts(uid(2), uid(3))[0].error_code, "model_error")

    def test_no_write_transaction_or_lock_during_generation(self) -> None:
        def generate(request):
            self.assertFalse(self.store._connection.in_transaction)
            # Another connection must actually be able to WRITE, not just read.
            with SQLiteStore(self.tmp.name) as other:
                other.create_session(uid(50))
            return FakeModel().generate(request)
        with patch.object(self.model, "generate", side_effect=generate):
            self.core.send(uid(2), self.input)
        self.assertEqual(self.core.get_session(uid(50)).turns, ())

    def test_mid_write_failure_rolls_back_history_receipt_event_and_revision(self) -> None:
        db = self.store._connection
        db.execute("""CREATE TEMP TRIGGER synthetic_abort BEFORE INSERT ON turns
            BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.core.send(uid(2), self.input)
        self.assertEqual(self.scalar("SELECT count(*) FROM turns"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM events WHERE kind='model_reply'"), 0)
        self.assertEqual(self.scalar("SELECT revision FROM sessions"), 0)
        self.assertIsNone(self.store.lookup(uid(2), self.input))
        self.assertEqual(self.store.attempts(uid(2), uid(3))[0].state, "running")
        db.execute("DROP TRIGGER synthetic_abort")
        self.core.send(uid(2), self.input)
        self.assertEqual([a.state for a in self.store.attempts(uid(2), uid(3))], ["unknown", "committed"])
        self.assertEqual(self.scalar("SELECT count(*) FROM turns"), 1)

    def test_interrupt_at_both_sides_of_durable_commit_and_retry(self) -> None:
        for phase in ("before", "after"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root:
                with SQLiteStore(root) as store:
                    model = FakeModel(["synthetic original", "synthetic retry"])
                    core = GenesisCore(store.identity, model, store=store)
                    core.create_session(uid(2))
                    commit = store._commit
                    def interrupted_commit():
                        publishing = store._connection.execute("SELECT count(*) FROM turns").fetchone()[0] == 1
                        if publishing and phase == "before":
                            raise KeyboardInterrupt("synthetic before commit")
                        commit()
                        if publishing and phase == "after":
                            raise KeyboardInterrupt("synthetic after commit")
                    with patch.object(store, "_commit", side_effect=interrupted_commit):
                        with self.assertRaises(KeyboardInterrupt):
                            core.send(uid(2), self.input)
                with SQLiteStore(root) as store:
                    model = FakeModel(["synthetic retry"] if phase == "before" else [])
                    core = GenesisCore(store.identity, model, store=store)
                    self.assertEqual(len(core.get_session(uid(2)).turns), 0 if phase == "before" else 1)
                    reply = core.send(uid(2), self.input)
                    self.assertEqual(reply.assistant.content, "synthetic retry" if phase == "before" else "synthetic original")
                    self.assertEqual(len(core.get_session(uid(2)).turns), 1)
                    self.assertEqual(len(model.requests), 1 if phase == "before" else 0)

    def test_retry_fences_old_worker_and_only_one_result_can_commit(self) -> None:
        old = self.store.begin_attempt(uid(2), self.input)
        with SQLiteStore(self.tmp.name) as other:
            new = other.begin_attempt(uid(2), self.input)
            self.assertEqual(self.store.attempts(uid(2), uid(3))[0].state, "unknown")
            from genesis.core import Turn
            from genesis.models.contracts import Message, Role
            def turn(content):
                return Turn(self.input, Message(Role.ASSISTANT, content), "synthetic-model")
            with self.assertRaises(StaleAttemptError):
                self.store.commit_turn(old, turn("synthetic old"))
            winner = other.commit_turn(new, turn("synthetic winner"))
            self.assertEqual(self.store.commit_turn(old, turn("synthetic old")), winner)
        self.assertEqual(len(self.core.get_session(uid(2)).turns), 1)

    def test_concurrent_different_inputs_cannot_commit_stale_context(self) -> None:
        from genesis.core import Turn
        from genesis.models.contracts import Message, Role
        a = self.store.begin_attempt(uid(2), self.input)
        other_input = UserInput(uid(4), "synthetic concurrent input")
        with SQLiteStore(self.tmp.name) as other:
            b = other.begin_attempt(uid(2), other_input)
            self.store.commit_turn(a, Turn(self.input, Message(Role.ASSISTANT, "synthetic a"), "fake"))
            with self.assertRaises(StaleAttemptError):
                other.commit_turn(b, Turn(other_input, Message(Role.ASSISTANT, "synthetic b"), "fake"))
            self.assertEqual(other.attempts(uid(2), uid(4))[0].state, "stale")
            core = GenesisCore(other.identity, FakeModel(), store=other)
            core.send(uid(2), other_input)
        self.assertEqual(len(self.core.get_session(uid(2)).turns), 2)

    def test_policy_denial_creates_no_input_attempt_or_turn(self) -> None:
        class Deny:
            def authorize(self, action):
                return PolicyDecision(False, "synthetic denial")
        core = GenesisCore(self.store.identity, self.model, Deny(), store=self.store)
        with self.assertRaises(PolicyDeniedError):
            core.send(uid(2), self.input)
        self.assertEqual(self.scalar("SELECT count(*) FROM requests"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM attempts"), 0)
        self.assertEqual(self.model.requests, ())

    def test_session_errors_and_identity_mismatch_are_explicit(self) -> None:
        with self.assertRaises(SessionExistsError):
            self.core.create_session(uid(2))
        with self.assertRaises(SessionNotFoundError):
            self.core.send(uid(99), self.input)
        with self.assertRaises(ValueError):
            GenesisCore(CompanionIdentity(uid(99)), self.model, store=self.store)

    def test_persistent_snapshots_do_not_leak_mutable_state(self) -> None:
        snapshot = self.core.get_session(uid(2))
        self.core.send(uid(2), self.input)
        current = self.core.get_session(uid(2))
        self.assertEqual(snapshot.turns, ())
        with self.assertRaises(FrozenInstanceError):
            current.turns[0].user_input.content = "synthetic rewrite"
        self.assertEqual(self.core.get_session(uid(2)), current)

    def test_database_errors_are_not_silently_ignored(self) -> None:
        self.store._connection.execute("PRAGMA query_only=ON")
        with self.assertRaises(sqlite3.OperationalError):
            self.core.send(uid(2), self.input)
        self.assertEqual(self.scalar("SELECT count(*) FROM requests"), 0)

    def test_failure_recording_error_surfaces_and_preserves_cause(self) -> None:
        def generate(request):
            self.store._connection.execute("PRAGMA query_only=ON")
            raise ModelError("synthetic adapter failure")
        with patch.object(self.model, "generate", side_effect=generate):
            with self.assertRaises(sqlite3.OperationalError) as caught:
                self.core.send(uid(2), self.input)
        self.assertIsInstance(caught.exception.__context__, ModelError)
        self.assertEqual(self.store.attempts(uid(2), uid(3))[0].state, "running")

    def test_sqlite_constraints_reject_orphans_and_duplicates(self) -> None:
        self.assertEqual(self.scalar("PRAGMA foreign_keys"), 1)
        with self.assertRaises(sqlite3.IntegrityError), self.store._transaction() as db:
            db.execute("INSERT INTO requests VALUES (?,?,?)", (str(uid(2)), str(uid(3)), str(uid(999))))
        self.core.send(uid(2), self.input)
        with self.assertRaises(sqlite3.IntegrityError), self.store._transaction() as db:
            db.execute("INSERT INTO turns SELECT * FROM turns")
        self.assertEqual(self.scalar("SELECT count(*) FROM turns"), 1)
        self.assertEqual(self.store._connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.scalar("PRAGMA integrity_check"), "ok")

    def test_persisted_evidence_cannot_promote_model_roots(self) -> None:
        self.core.send(uid(2), self.input)
        user, model = self.store.events(uid(2))
        echo = self.store.record_derived(uid(2), model.reference.event_id, kind="tool_echo")
        summary = self.store.record_derived(uid(2), user.reference.event_id,
                                            kind="summary", content="synthetic summary")
        inference = self.store.record_derived(uid(2), summary.event_id,
                                              kind="inference", content="synthetic inference")
        self.store.close()
        with SQLiteStore(self.tmp.name) as reopened:
            registry = reopened.evidence_registry(uid(2))
            self.assertEqual(registry.independent_roots((echo, model.reference)), frozenset())
            self.assertEqual(registry.independent_roots((summary, inference)), frozenset({user.reference.event_id}))
            forged = EvidenceReference(model.reference.event_id, EvidenceOrigin.USER_INPUT, model.reference.event_id)
            self.assertEqual(registry.independent_roots((forged,)), frozenset())
            with self.assertRaises(TypeError):
                reopened.record_derived(uid(2), model.reference.event_id, kind="summary",
                                        content="synthetic forged", origin=EvidenceOrigin.USER_INPUT)

    def test_invalid_derivations_and_cross_session_roots_are_rejected(self) -> None:
        self.core.send(uid(2), self.input)
        user, model = self.store.events(uid(2))
        self.core.create_session(uid(9))
        with self.assertRaises(ValueError):
            self.store.record_derived(uid(9), model.reference.event_id, kind="tool_echo")
        with self.assertRaises(ValueError):
            self.store.record_derived(uid(2), model.reference.event_id, kind="tool_echo", content="synthetic replacement")
        with self.assertRaises(sqlite3.IntegrityError), self.store._transaction() as db:
            self.store._event(db, uid(90), uid(2), "tool_echo", "tool_result",
                              user.reference.event_id, model.reference.event_id, "synthetic laundering")
        with self.assertRaises(sqlite3.IntegrityError), self.store._transaction() as db:
            self.store._event(db, uid(91), uid(2), "summary", "user_input",
                              user.reference.event_id, user.reference.event_id, "synthetic spoof")
