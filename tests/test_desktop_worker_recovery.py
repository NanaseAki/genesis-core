"""Synthetic restart/recovery checks; workers own every live SQLite handle."""

from contextlib import contextmanager, nullcontext
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from uuid import uuid4

from genesis.adapters.desktop_worker import ChatWorker, WorkerStateError
from genesis.adapters.desktop import DesktopSession
from genesis.core import GenesisCore, UserInput
from genesis.models.contracts import Message, ModelError, Role
from genesis.models.fake import FakeModel
from genesis.storage._initialization import InitializationBusyError
from genesis.storage.sqlite import SQLiteStore


class DesktopRecoveryTests(unittest.TestCase):
    def receive(self, worker):
        self.assertTrue(worker.wait_for_events(5), "no synthetic worker event")
        return worker.poll_events()

    @contextmanager
    def worker(self, root=None, *, model=None, mode="open", **kwargs):
        model = FakeModel() if model is None else model
        factory = (None if root is None else
                   lambda: SQLiteStore(root, data_kind="local_preview", mode=mode))
        worker = ChatWorker(lambda: nullcontext(model), store_factory=factory, **kwargs)
        try:
            ready = self.receive(worker)
            self.assertEqual([event.kind for event in ready], ["ready"])
            yield worker, model, ready[0]
        finally:
            worker.close()
            self.assertTrue(worker.join(5), "synthetic recovery worker did not close")
            worker.poll_events()

    def test_startup_validation_precedes_thread_and_resource_acquisition(self):
        acquired = []

        def factory():
            acquired.append(True)
            return nullcontext(FakeModel())

        bad_options = (
            {"startup": "browse"}, {"startup": "resume", "resume_session_id": uuid4()},
            {"startup": "invalid"}, {"resume_session_id": uuid4()},
            {"startup": "resume", "store_factory": factory},
            {"startup": "browse", "store_factory": factory, "resume_session_id": uuid4()},
            {"startup": "resume", "store_factory": factory, "resume_session_id": "not-a-uuid"},
        )
        for options in bad_options:
            with self.subTest(options=options):
                with self.assertRaises((ValueError, TypeError)):
                    ChatWorker(factory, **options)
        self.assertEqual(acquired, [])

    def test_browse_lists_only_existing_sessions_without_creation_or_generation(self):
        with tempfile.TemporaryDirectory(prefix="genesis-recovery-synthetic-") as folder:
            root = Path(folder) / "preview"
            with SQLiteStore(root, data_kind="local_preview", mode="create") as store:
                identities = [store.create_session(uuid4()).session_id for _ in range(2)]
            with self.worker(root, startup="browse") as (worker, model, event):
                self.assertIsNone(event.session_id)
                self.assertEqual(event.history, ())
                self.assertEqual(event.pending_requests, ())
                self.assertEqual(event.session_ids, tuple(identities))
                self.assertEqual(model.requests, ())
                self.assertFalse(worker.can_retry)
                with self.assertRaises(WorkerStateError):
                    worker.submit("synthetic browse must not send")
                with self.assertRaises(WorkerStateError):
                    worker.retry(uuid4())
            with SQLiteStore(root, data_kind="local_preview", mode="open") as store:
                self.assertEqual(store.session_ids(), tuple(identities))

    def test_new_session_is_explicit_and_resume_supplies_exact_committed_history(self):
        with tempfile.TemporaryDirectory(prefix="genesis-recovery-synthetic-") as folder:
            root = Path(folder) / "preview"
            with self.worker(root, mode="create") as (worker, model, event):
                session_id = event.session_id
                self.assertIsNotNone(session_id)
                self.assertEqual(event.session_ids, (session_id,))
                self.assertEqual(event.history, ())
                original = worker.submit("  synthetic first\n保持原文  ")
                first = self.receive(worker)[0]
                self.assertEqual(first.history, (first.turn,))
                self.assertEqual(first.session_id, session_id)
            with self.worker(root, startup="resume", resume_session_id=session_id) as (worker, model, event):
                self.assertEqual(event.history, (first.turn,))
                self.assertEqual(event.history[0].user_input, original)
                self.assertEqual(event.pending_requests, ())
                self.assertEqual(model.requests, ())
                with self.assertRaises(FrozenInstanceError):
                    event.history = ()
                worker.submit("synthetic follow-up")
                second = self.receive(worker)[0]
                self.assertEqual(len(second.history), 2)
                self.assertEqual(model.requests[0].messages[:2],
                                 (Message(Role.USER, original.content), first.turn.assistant))
            with SQLiteStore(root, data_kind="local_preview", mode="open") as store:
                self.assertEqual(store.session_ids(), (session_id,))
                self.assertEqual(store.get_session(session_id).turns, second.history)

    def test_committed_id_replay_after_restart_never_invokes_model(self):
        with tempfile.TemporaryDirectory(prefix="genesis-recovery-synthetic-") as folder:
            root = Path(folder) / "preview"
            with self.worker(root, mode="create") as (worker, model, event):
                original = worker.submit("synthetic committed original")
                successful = self.receive(worker)[0]
                session_id = event.session_id
            with self.worker(root, startup="resume", resume_session_id=session_id,
                             model=FakeModel([AssertionError("replay must not generate")])) as (worker, model, event):
                self.assertEqual(worker.retry(original.input_id), original)
                replayed = self.receive(worker)[0]
                self.assertEqual(replayed.kind, "succeeded")
                self.assertEqual(replayed.turn, successful.turn)
                self.assertEqual(replayed.history, (successful.turn,))
                self.assertEqual(model.requests, ())
                self.assertFalse(worker.can_retry)

    def test_restored_failed_input_requires_explicit_id_and_preserves_original_text(self):
        with tempfile.TemporaryDirectory(prefix="genesis-recovery-synthetic-") as folder:
            root = Path(folder) / "preview"
            with self.worker(root, mode="create", model=FakeModel([ModelError("synthetic failure")])) as (
                    worker, model, event):
                original = worker.submit("  synthetic failed\n不可修改  ")
                failure = self.receive(worker)[0]
                session_id = event.session_id
                self.assertFalse(failure.recovered_commit)
                self.assertEqual(failure.pending_requests[0].user_input, original)
                self.assertEqual(failure.pending_requests[0].attempts[-1].state, "failed")
            with self.worker(root, startup="resume", resume_session_id=session_id) as (worker, model, event):
                pending = event.pending_requests[0]
                self.assertEqual(event.history, ())
                self.assertEqual(pending.user_input, original)
                self.assertEqual(pending.attempts[-1].state, "failed")
                self.assertEqual(model.requests, ())
                self.assertFalse(worker.can_retry)
                with self.assertRaises(WorkerStateError):
                    worker.retry()
                with self.assertRaises(FrozenInstanceError):
                    pending.user_input.content = "synthetic altered"
                self.assertEqual(worker.retry(original.input_id), original)
                success = self.receive(worker)[0]
                self.assertEqual(success.turn.user_input, original)
                self.assertEqual(success.pending_requests, ())
                self.assertEqual(len(model.requests), 1)
                self.assertEqual(model.requests[0].messages[-1].content, original.content)

    def test_running_attempt_is_not_fenced_on_restore_but_only_on_explicit_retry(self):
        with tempfile.TemporaryDirectory(prefix="genesis-recovery-synthetic-") as folder:
            root = Path(folder) / "preview"
            original = UserInput(uuid4(), "synthetic execution outcome unknown")
            with SQLiteStore(root, data_kind="local_preview", mode="create") as store:
                session_id = store.create_session(uuid4()).session_id
                attempt = store.begin_attempt(session_id, original)
            with self.worker(root, startup="resume", resume_session_id=session_id) as (worker, model, event):
                status = event.pending_requests[0].attempts[-1]
                self.assertEqual(status.attempt_id, attempt.attempt_id)
                self.assertEqual(status.state, "running")
                self.assertEqual(model.requests, ())
            with SQLiteStore(root, data_kind="local_preview", mode="open") as store:
                statuses = store.attempts(session_id, original.input_id)
                self.assertEqual(len(statuses), 1)
                self.assertEqual(statuses[0].state, "running")
            with self.worker(root, startup="resume", resume_session_id=session_id) as (worker, model, event):
                self.assertEqual(worker.retry(original.input_id), original)
                success = self.receive(worker)[0]
                self.assertEqual(success.kind, "succeeded")
                self.assertEqual(len(model.requests), 1)
            with SQLiteStore(root, data_kind="local_preview", mode="open") as store:
                statuses = store.attempts(session_id, original.input_id)
                self.assertEqual([item.state for item in statuses], ["unknown", "committed"])

    def test_retry_cannot_address_another_session_or_supply_changed_text(self):
        with tempfile.TemporaryDirectory(prefix="genesis-recovery-synthetic-") as folder:
            root = Path(folder) / "preview"
            with SQLiteStore(root, data_kind="local_preview", mode="create") as store:
                core = GenesisCore(store.identity, FakeModel(), store=store)
                selected, other = core.create_session(), core.create_session()
                foreign = UserInput(uuid4(), "synthetic other session")
                core.send(other.session_id, foreign)
            with self.worker(root, startup="resume", resume_session_id=selected.session_id) as (worker, model, event):
                self.assertEqual(event.history, ())
                self.assertEqual(event.session_ids, (selected.session_id, other.session_id))
                with self.assertRaises(WorkerStateError):
                    worker.retry(foreign.input_id)
                with self.assertRaises(TypeError):
                    worker.retry(str(foreign.input_id))
                with self.assertRaises(TypeError):
                    worker.retry(foreign.input_id, "synthetic changed text")
                self.assertEqual(model.requests, ())
                self.assertEqual(worker.state, "ready")

    def test_missing_resume_session_is_fatal_and_does_not_create_replacement(self):
        with tempfile.TemporaryDirectory(prefix="genesis-recovery-synthetic-") as folder:
            root = Path(folder) / "preview"
            with SQLiteStore(root, data_kind="local_preview", mode="create") as store:
                existing = store.create_session(uuid4()).session_id
            model = FakeModel()
            worker = ChatWorker(lambda: nullcontext(model),
                                store_factory=lambda: SQLiteStore(root, data_kind="local_preview", mode="open"),
                                startup="resume", resume_session_id=uuid4())
            try:
                self.assertTrue(worker.join(5))
                events = worker.poll_events()
                self.assertEqual([event.kind for event in events], ["fatal", "closed"])
                self.assertEqual(events[0].error_code, "session_startup_error")
                self.assertEqual(worker.state, "fatal")
                self.assertEqual(model.requests, ())
            finally:
                worker.close()
                self.assertTrue(worker.join(5))
            with SQLiteStore(root, data_kind="local_preview", mode="open") as store:
                self.assertEqual(store.session_ids(), (existing,))

    def test_recovery_discovery_reads_and_close_all_use_worker_thread(self):
        with tempfile.TemporaryDirectory(prefix="genesis-recovery-synthetic-") as folder:
            root = Path(folder) / "preview"
            with SQLiteStore(root, data_kind="local_preview", mode="create") as store:
                session_id = store.create_session(uuid4()).session_id
                pending = store.begin_attempt(session_id, UserInput(uuid4(), "synthetic thread-owned pending"))
                store.fail_attempt(pending.attempt_id, "interrupted", "generation_interrupted")
            calls = []

            class TracedStore(SQLiteStore):
                def __init__(self):
                    calls.append(("open", threading.get_ident()))
                    super().__init__(root, data_kind="local_preview", mode="open")

                def recovery_snapshot(self, session):
                    calls.append(("recovery", threading.get_ident()))
                    return super().recovery_snapshot(session)

                def session_ids(self):
                    calls.append(("catalog", threading.get_ident()))
                    return super().session_ids()

                def close(self):
                    calls.append(("close", threading.get_ident()))
                    super().close()

            model = FakeModel()
            worker = ChatWorker(lambda: nullcontext(model), store_factory=TracedStore,
                                startup="resume", resume_session_id=session_id)
            try:
                event = self.receive(worker)[0]
                self.assertEqual(event.pending_requests[0].attempts[-1].state, "interrupted")
                self.assertEqual(model.requests, ())
            finally:
                worker.close()
                self.assertTrue(worker.join(5))
                worker.poll_events()
            self.assertEqual({item[0] for item in calls}, {"open", "recovery", "catalog", "close"})
            self.assertEqual(len({item[1] for item in calls}), 1)
            self.assertNotEqual(calls[0][1], threading.get_ident())

    def test_startup_return_loss_preserves_written_data_and_reports_safe_stage(self):
        # A startup exception need not mean nothing was written. Both cases
        # preserve their actual committed data and permit later explicit open.
        for stage in ("storage", "session"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(
                    prefix="genesis-recovery-synthetic-") as folder:
                root = Path(folder) / "preview"
                marker = "SYNTHETIC_PRIVATE_STARTUP_RETURN_LOSS"

                class ReturnLostStore(SQLiteStore):
                    def create_session(self, session_id):
                        result = super().create_session(session_id)
                        if stage == "session":
                            raise RuntimeError(marker)
                        return result

                @contextmanager
                def factory():
                    with ReturnLostStore(root, data_kind="local_preview", mode="create") as store:
                        if stage == "storage":
                            raise RuntimeError(marker)
                        yield store

                model = FakeModel()
                worker = ChatWorker(lambda: nullcontext(model), store_factory=factory)
                try:
                    self.assertTrue(worker.join(5))
                    events = worker.poll_events()
                    self.assertEqual([event.kind for event in events], ["fatal", "closed"])
                    self.assertEqual(events[0].error_code, stage + "_startup_error")
                    self.assertNotIn(marker, repr(events))
                    self.assertEqual(model.requests, ())
                finally:
                    worker.close()
                    self.assertTrue(worker.join(5))
                with SQLiteStore(root, data_kind="local_preview", mode="open") as store:
                    self.assertEqual(len(store.session_ids()), 0 if stage == "storage" else 1)

    def test_initialization_busy_is_safe_specific_to_store_startup_and_closes_resources(self):
        marker = "SYNTHETIC_PRIVATE_INITIALIZATION_BUSY"
        for stage in ("model", "storage_call", "storage_enter", "session"):
            with self.subTest(stage=stage):
                model, lifecycle = FakeModel(), []

                def record(action):
                    lifecycle.append((action, threading.get_ident()))

                class UnusableStore:
                    @property
                    def identity(self):
                        raise InitializationBusyError(marker)

                @contextmanager
                def model_factory():
                    record("model_enter")
                    try:
                        if stage == "model":
                            raise InitializationBusyError(marker)
                        yield model
                    finally:
                        record("model_exit")

                @contextmanager
                def store_context():
                    record("store_enter")
                    try:
                        if stage == "storage_enter":
                            raise InitializationBusyError(marker) from ValueError(marker + " cause")
                        yield UnusableStore()
                    finally:
                        record("store_exit")

                def store_factory():
                    record("store_factory")
                    if stage == "storage_call":
                        raise InitializationBusyError(marker)
                    return store_context()

                worker = ChatWorker(model_factory, store_factory=store_factory)
                try:
                    self.assertTrue(worker.join(5))
                    events = worker.poll_events()
                    expected = {"model": "worker_startup_error", "session": "session_startup_error"}.get(
                        stage, "storage_initialization_busy")
                    self.assertEqual([event.kind for event in events], ["fatal", "closed"])
                    self.assertTrue(all(event.error_code == expected for event in events))
                    self.assertNotIn(marker, repr(events))
                    presenter = DesktopSession(SimpleNamespace(poll_events=lambda: events), Mock())
                    presenter.pump()
                    self.assertEqual(presenter.state.phase, "fatal")
                    self.assertNotIn(marker, presenter.state.error)
                    if stage.startswith("storage"):
                        self.assertIn("本次未创建数据库", presenter.state.error)
                        self.assertIn("请勿删除锁文件", presenter.state.error)
                        self.assertIn("不会自动重试", presenter.state.error)
                    else:
                        self.assertNotIn("本次未创建数据库", presenter.state.error)
                    self.assertFalse(worker.can_retry)
                    with self.assertRaises(WorkerStateError):
                        worker.retry()
                    self.assertEqual(model.requests, ())
                    actions = [action for action, _ in lifecycle]
                    self.assertEqual(actions[0], "model_enter")
                    self.assertEqual(actions[-1], "model_exit")
                    self.assertEqual(actions.count("store_factory"), int(stage != "model"))
                    self.assertEqual(actions.count("store_enter"), int(stage in {"storage_enter", "session"}))
                    self.assertEqual(actions.count("store_exit"), actions.count("store_enter"))
                    self.assertEqual(len({owner for _, owner in lifecycle}), 1)
                    self.assertNotEqual(lifecycle[0][1], threading.get_ident())
                finally:
                    worker.close()
                    self.assertTrue(worker.join(5))

    def test_failed_generation_recovers_other_connection_committed_receipt_without_retry(self):
        entered, release = threading.Event(), threading.Event()

        class DelayedFailure(FakeModel):
            def __init__(self):
                super().__init__([ModelError("synthetic delayed model failure")])

            def generate(self, request):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("synthetic model release was not signalled")
                return super().generate(request)

        with tempfile.TemporaryDirectory(prefix="genesis-recovery-synthetic-") as folder:
            root = Path(folder) / "preview"
            snapshots = []

            class ObservedStore(SQLiteStore):
                def recovery_snapshot(self, session_id):
                    result = super().recovery_snapshot(session_id)
                    snapshots.append(result)
                    return result

            model = DelayedFailure()
            worker = ChatWorker(lambda: nullcontext(model), store_factory=lambda: ObservedStore(
                root, data_kind="local_preview", mode="create"))
            try:
                ready = self.receive(worker)[0]
                self.assertEqual(ready.kind, "ready")
                original = worker.submit("  synthetic concurrent original\n保持原文  ")
                self.assertTrue(entered.wait(5), "worker did not enter model generation")
                winning_model = FakeModel(["synthetic already committed winner"])
                # This is an independent real SQLite connection on its own
                # thread. Explicitly retrying the original ID fences the old
                # attempt, then commits the winner while that model is blocked.
                with SQLiteStore(root, data_kind="local_preview", mode="open") as other:
                    winner = GenesisCore(other.identity, winning_model, store=other).send(
                        ready.session_id, original)
                release.set()
                events = self.receive(worker)
                self.assertEqual([event.kind for event in events], ["succeeded"])
                event = events[0]
                self.assertEqual(event.turn, winner)
                self.assertEqual(event.history, (winner,))
                self.assertEqual(event.pending_requests, ())
                self.assertTrue(event.recovered_commit)
                self.assertIsNone(event.error_code)
                self.assertFalse(worker.can_retry)
                self.assertEqual(worker.state, "ready")
                with self.assertRaises(WorkerStateError):
                    worker.retry()
                self.assertEqual(len(model.requests), 1)
                self.assertEqual(len(winning_model.requests), 1)
                # Initial ready plus one completion snapshot, not a second
                # recovery read that could observe another publication.
                self.assertEqual(len(snapshots), 2)
                self.assertEqual(event.history, snapshots[-1].session.turns)
                self.assertEqual(event.pending_requests, snapshots[-1].unfinished_requests)
            finally:
                release.set()
                worker.close()
                self.assertTrue(worker.join(6), "synthetic concurrent worker did not close")
                worker.poll_events()
            with SQLiteStore(root, data_kind="local_preview", mode="open") as store:
                self.assertEqual(store.get_session(ready.session_id).turns, (winner,))
                self.assertEqual([item.state for item in store.attempts(ready.session_id, original.input_id)],
                                 ["unknown", "committed"])


if __name__ == "__main__":
    unittest.main()
