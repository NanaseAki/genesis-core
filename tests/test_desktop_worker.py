"""Synthetic UI-worker lifecycle tests, synchronized with events, not sleeps."""

from contextlib import contextmanager, nullcontext, redirect_stderr
from dataclasses import FrozenInstanceError
from io import StringIO
from pathlib import Path
import tempfile
import threading
import traceback
import unittest
from unittest.mock import patch

from genesis.adapters.desktop_worker import ChatWorker, WorkerStateError
from genesis.models.contracts import ModelError, ModelReply
from genesis.models.fake import FakeModel
from genesis.models.ollama import OllamaError, OllamaFailure
from genesis.storage.sqlite import SQLiteStore
from genesis.text import TextContractError


class BlockingModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate(self, request):
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("synthetic test release was not signalled")
        return super().generate(request)


class ChatWorkerTests(unittest.TestCase):
    def worker(self, model=None, **kwargs):
        model = FakeModel() if model is None else model
        worker = ChatWorker(lambda: nullcontext(model), **kwargs)
        self.addCleanup(self.finish, worker)
        events = self.receive(worker)
        self.assertEqual([event.kind for event in events], ["ready"])
        self.assertEqual(worker.state, "ready")
        return worker, model

    def finish(self, worker):
        worker.close()
        self.assertTrue(worker.join(6), "synthetic worker did not finish")
        worker.poll_events()

    def receive(self, worker):
        self.assertTrue(worker.wait_for_events(5), "no worker event")
        return worker.poll_events()

    def test_success_preserves_exact_text_and_supplies_history(self):
        worker, model = self.worker()
        first = worker.submit("  synthetic first\nsecond line  ")
        result = self.receive(worker)[0]
        self.assertEqual(result.kind, "succeeded")
        self.assertEqual(result.turn.user_input, first)
        self.assertEqual(result.turn.assistant.content, "Fake response: " + first.content)
        self.assertEqual(model.requests[0].messages[-1].content, first.content)
        second = worker.submit("synthetic follow-up")
        result2 = self.receive(worker)[0]
        self.assertEqual(result2.input_id, second.input_id)
        self.assertEqual(len(model.requests[1].messages), 3)
        self.assertEqual(model.requests[1].messages[0].content, first.content)
        self.assertEqual(model.requests[1].messages[1].content, result.turn.assistant.content)
        self.assertEqual(worker.state, "ready")
        with self.assertRaises(FrozenInstanceError):
            result2.error_code = "changed"

    def test_duplicate_send_and_retry_rejected_until_completion_consumed(self):
        model = BlockingModel()
        worker, _ = self.worker(model)
        self.addCleanup(model.release.set)
        worker.submit("synthetic click")
        self.assertTrue(model.entered.wait(5))
        with self.assertRaises(WorkerStateError):
            worker.submit("synthetic click")
        with self.assertRaises(WorkerStateError):
            worker.retry()
        model.release.set()
        self.assertTrue(worker.wait_for_events(5))
        self.assertEqual(worker.state, "busy")
        with self.assertRaises(WorkerStateError):
            worker.submit("synthetic click")
        self.assertEqual([e.kind for e in worker.poll_events()], ["succeeded"])
        self.assertEqual(len(model.requests), 1)

    def test_explicit_retry_keeps_input_id_and_exact_original_content(self):
        model = FakeModel([ModelError("synthetic-private-error"), "synthetic recovered"])
        worker, _ = self.worker(model)
        original = worker.submit("  synthetic retry\n ")
        failed = self.receive(worker)[0]
        self.assertEqual(failed.kind, "failed")
        self.assertEqual(failed.error_code, "model_error")
        self.assertNotIn("synthetic-private-error", repr(failed))
        self.assertNotIn(original.content, repr(failed))
        self.assertTrue(worker.can_retry)
        self.assertEqual(len(model.requests), 1)
        retried = worker.retry()
        self.assertIs(retried, original)
        succeeded = self.receive(worker)[0]
        self.assertEqual(succeeded.turn.assistant.content, "synthetic recovered")
        self.assertEqual(model.requests[0], model.requests[1])
        self.assertFalse(worker.can_retry)
        with self.assertRaises(WorkerStateError):
            worker.retry()

    def test_edit_after_failure_is_new_input_and_does_not_archive_failed_text(self):
        model = FakeModel([ModelError("synthetic failure"), "synthetic reply"])
        worker, _ = self.worker(model)
        failed = worker.submit("synthetic abandoned")
        self.receive(worker)
        edited = worker.submit("synthetic revised")
        self.assertNotEqual(failed.input_id, edited.input_id)
        self.receive(worker)
        self.assertEqual(len(model.requests[1].messages), 1)
        self.assertEqual(model.requests[1].messages[0].content, edited.content)
        self.assertFalse(worker.can_retry)

    def test_invalid_text_is_not_queued_or_normalized(self):
        worker, model = self.worker()
        for invalid in ("", " \n\t", "synthetic\0text", "\ud800", 123):
            with self.subTest(invalid=repr(invalid)):
                with self.assertRaises((TextContractError, TypeError)):
                    worker.submit(invalid)
                self.assertEqual(worker.state, "ready")
                self.assertEqual(model.requests, ())

    def test_ollama_failure_exposes_only_allowlisted_category(self):
        worker, _ = self.worker(FakeModel([OllamaError(OllamaFailure.READ_TIMEOUT)]))
        worker.submit("synthetic timeout")
        event = self.receive(worker)[0]
        self.assertEqual(event.kind, "failed")
        self.assertEqual(event.error_code, "read_timeout")
        self.assertIsNone(event.turn)

    def test_close_returns_while_inflight_and_waits_for_owned_cleanup(self):
        model = BlockingModel()
        ownership = []

        @contextmanager
        def factory():
            ownership.append(("enter", threading.get_ident()))
            try:
                yield model
            finally:
                ownership.append(("exit", threading.get_ident()))

        worker = ChatWorker(factory)
        self.addCleanup(self.finish, worker)
        self.addCleanup(model.release.set)
        self.receive(worker)
        worker.submit("synthetic pending")
        self.assertTrue(model.entered.wait(5))
        worker.close()
        worker.close()
        self.assertTrue(worker.is_alive)
        self.assertFalse(worker.join())
        self.assertEqual(worker.state, "closing")
        self.assertEqual([entry[0] for entry in ownership], ["enter"])
        with self.assertRaises(WorkerStateError):
            worker.submit("synthetic too late")
        model.release.set()
        self.assertTrue(worker.join(5))
        events = worker.poll_events()
        self.assertEqual([event.kind for event in events], ["succeeded", "closed"])
        self.assertEqual(worker.state, "closed")
        self.assertEqual(ownership[0][1], ownership[1][1])
        self.assertNotEqual(ownership[0][1], threading.get_ident())

    def test_close_during_factory_does_not_reopen_sending_on_ready_event(self):
        entered, release = threading.Event(), threading.Event()

        @contextmanager
        def factory():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("synthetic startup release missing")
            yield FakeModel()

        worker = ChatWorker(factory)
        self.addCleanup(self.finish, worker)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(5))
        worker.close()
        release.set()
        self.assertTrue(worker.join(5))
        self.assertEqual([e.kind for e in worker.poll_events()], ["ready", "closed"])
        self.assertEqual(worker.state, "closed")

    def test_unexpected_generation_error_is_fatal_not_retryable(self):
        class BrokenModel(FakeModel):
            def generate(self, request):
                raise RuntimeError("synthetic-private-programming-error")

        worker, _ = self.worker(BrokenModel())
        user_input = worker.submit("synthetic fatal")
        self.assertTrue(worker.join(5))
        events = worker.poll_events()
        self.assertEqual([e.kind for e in events], ["fatal", "closed"])
        self.assertEqual(events[0].input_id, user_input.input_id)
        self.assertEqual(events[0].error_code, "unexpected_error")
        self.assertNotIn("synthetic-private-programming-error", repr(events))
        self.assertEqual(worker.state, "fatal")
        self.assertFalse(worker.can_retry)
        with self.assertRaises(WorkerStateError):
            worker.retry()

    def test_factory_failure_is_reported_without_raw_payload(self):
        def broken_factory():
            raise RuntimeError("synthetic-private-startup-error")

        worker = ChatWorker(broken_factory)
        self.addCleanup(self.finish, worker)
        self.assertTrue(worker.join(5))
        events = worker.poll_events()
        self.assertEqual([e.kind for e in events], ["fatal", "closed"])
        self.assertNotIn("synthetic-private-startup-error", repr(events))
        self.assertEqual(worker.state, "fatal")

    def test_constructor_failure_before_native_start_preserves_exception_without_resources(self):
        real_start = threading.Thread.start
        for error_type in (RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(error_type=error_type.__name__):
                marker = error_type("synthetic pre-native start failure")
                workers, resources = [], []

                @contextmanager
                def factory():
                    resources.append(threading.get_ident())
                    yield FakeModel()

                def fail_before_start(thread):
                    if thread.name != "genesis-chat":
                        return real_start(thread)
                    workers.append(thread._target.__self__)
                    raise marker

                with patch.object(threading.Thread, "start", fail_before_start), patch.object(
                        threading.Thread, "join", side_effect=AssertionError("constructor must not join")):
                    with self.assertRaises(error_type) as caught:
                        ChatWorker(factory)
                self.assertIs(caught.exception, marker)
                self.assertEqual(len(workers), 1)
                self.assertEqual(workers[0].state, "closing")
                self.assertFalse(workers[0]._thread._started.is_set())
                self.assertFalse(workers[0].is_alive)
                self.assertEqual(resources, [])

    def test_native_start_wait_failure_closes_real_worker_before_fallback_cleanup(self):
        real_start = threading.Thread.start
        for error_type in (RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(error_type=error_type.__name__):
                marker = error_type("synthetic native start wait failure")
                entered = threading.Event()
                workers, ownership = [], []

                @contextmanager
                def factory():
                    ownership.append(("enter", threading.get_ident()))
                    entered.set()
                    try:
                        yield FakeModel()
                    finally:
                        ownership.append(("exit", threading.get_ident()))

                def interrupt_native_wait(thread):
                    if thread.name != "genesis-chat":
                        return real_start(thread)
                    workers.append(thread._target.__self__)
                    real_wait = thread._started.wait

                    def wait_then_fail(*args, **kwargs):
                        real_wait(*args, **kwargs)
                        if not entered.wait(5):
                            raise AssertionError("synthetic worker did not acquire its resource")
                        raise marker

                    thread._started.wait = wait_then_fail
                    try:
                        return real_start(thread)
                    finally:
                        thread._started.wait = real_wait

                try:
                    with patch.object(threading.Thread, "start", interrupt_native_wait), patch.object(
                            threading.Thread, "join", side_effect=AssertionError("constructor must not join")):
                        with self.assertRaises(error_type) as caught:
                            ChatWorker(factory)
                    self.assertIs(caught.exception, marker)
                    self.assertEqual(len(workers), 1)
                    # This must pass before fallback close() in finally; that
                    # fallback prevents a regression from hanging the runner.
                    self.assertTrue(workers[0].join(5), "constructor leaked its started worker")
                    self.assertFalse(workers[0].is_alive)
                    self.assertEqual([item[0] for item in ownership], ["enter", "exit"])
                    self.assertEqual(ownership[0][1], ownership[1][1])
                    self.assertNotEqual(ownership[0][1], threading.get_ident())
                    events = workers[0].poll_events()
                    self.assertEqual([event.kind for event in events], ["ready", "closed"])
                    self.assertEqual(workers[0].state, "closed")
                finally:
                    for worker in workers:
                        worker.close()
                        self.assertTrue(worker.join(5), "fallback worker cleanup failed")

    def test_native_start_wait_failure_before_started_signal_stops_late_run(self):
        real_start = threading.Thread.start
        real_bootstrap = threading.Thread._bootstrap_inner
        marker = KeyboardInterrupt("synthetic interrupt before the started signal")
        bootstrap_entered, release_bootstrap = threading.Event(), threading.Event()
        workers, resources, bootstrap_timeouts = [], [], []

        @contextmanager
        def factory():
            resources.append(threading.get_ident())
            yield FakeModel()

        def delayed_bootstrap(thread):
            if thread.name == "genesis-chat":
                bootstrap_entered.set()
                if not release_bootstrap.wait(5):
                    bootstrap_timeouts.append(True)
            return real_bootstrap(thread)

        def interrupt_native_wait(thread):
            if thread.name != "genesis-chat":
                return real_start(thread)
            workers.append(thread._target.__self__)
            real_wait = thread._started.wait

            def fail_before_started(*args, **kwargs):
                if not bootstrap_entered.wait(5):
                    raise AssertionError("native bootstrap did not begin")
                self.assertFalse(thread._started.is_set())
                raise marker

            thread._started.wait = fail_before_started
            try:
                return real_start(thread)
            finally:
                thread._started.wait = real_wait

        try:
            with patch.object(threading.Thread, "_bootstrap_inner", delayed_bootstrap), patch.object(
                    threading.Thread, "start", interrupt_native_wait), patch.object(
                    threading.Thread, "join", side_effect=AssertionError("constructor must not join")):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    ChatWorker(factory)
                self.assertIs(caught.exception, marker)
                self.assertEqual(len(workers), 1)
                self.assertEqual(workers[0].state, "closing")
                self.assertFalse(workers[0]._thread._started.is_set())
                self.assertEqual(resources, [])
                release_bootstrap.set()
            self.assertTrue(workers[0]._thread._started.wait(5))
            # Exit is required before any fallback cleanup request, even though
            # the native thread was invisible to is_alive() at the interruption.
            self.assertTrue(workers[0].join(5), "late worker ignored constructor cleanup")
            self.assertEqual(resources, [])
            self.assertEqual(bootstrap_timeouts, [])
            self.assertEqual([event.kind for event in workers[0].poll_events()], ["closed"])
            self.assertEqual(workers[0].state, "closed")
        finally:
            release_bootstrap.set()
            for worker in workers:
                worker.close()
                self.assertTrue(worker._thread._started.wait(5), "native bootstrap cleanup failed")
                self.assertTrue(worker.join(5), "fallback worker cleanup failed")

    def test_keyboard_interrupt_cleans_up_and_reaches_thread_hook(self):
        cleaned = threading.Event()
        hook_called = threading.Event()
        hook_types = []

        class InterruptedModel(FakeModel):
            def generate(self, request):
                raise KeyboardInterrupt()

        @contextmanager
        def factory():
            try:
                yield InterruptedModel()
            finally:
                cleaned.set()

        previous_hook = threading.excepthook

        def capture_hook(args):
            hook_types.append(args.exc_type)
            hook_called.set()

        threading.excepthook = capture_hook
        self.addCleanup(setattr, threading, "excepthook", previous_hook)
        worker = ChatWorker(factory)
        self.addCleanup(self.finish, worker)
        self.receive(worker)
        worker.submit("synthetic interruption")
        self.assertTrue(hook_called.wait(5))
        self.assertTrue(worker.join(5))
        self.assertTrue(cleaned.is_set())
        self.assertEqual(hook_types, [KeyboardInterrupt])
        events = worker.poll_events()
        self.assertEqual([e.kind for e in events], ["fatal", "closed"])
        self.assertEqual(events[0].error_code, "worker_interrupted")

    def test_builtin_interruptions_reach_default_hook_without_private_diagnostics(self):
        for interrupt_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(interrupt_type=interrupt_type.__name__):
                marker = "SYNTHETIC_PRIVATE_INTERRUPT_PAYLOAD"
                cleaned, hook_called = threading.Event(), threading.Event()
                originals, forwarded, formatted = [], [], []

                class InterruptedModel(FakeModel):
                    def generate(self, request):
                        failure = interrupt_type(marker)
                        failure.add_note(marker + " note")
                        originals.append(failure)
                        try:
                            raise RuntimeError(marker + " context")
                        except RuntimeError:
                            raise failure from ValueError(marker + " cause")

                @contextmanager
                def factory():
                    try:
                        yield InterruptedModel()
                    finally:
                        cleaned.set()

                def capture_default_hook(args):
                    try:
                        forwarded.append(args.exc_value)
                        formatted.append("".join(traceback.format_exception(
                            args.exc_type, args.exc_value, args.exc_traceback)))
                        # Exercise the real standard hook, including its special
                        # handling of SystemExit, without changing production code.
                        threading.__excepthook__(args)
                    finally:
                        hook_called.set()

                stderr = StringIO()
                with redirect_stderr(stderr), patch("threading.excepthook", capture_default_hook):
                    worker = ChatWorker(factory)
                    self.addCleanup(self.finish, worker)
                    self.receive(worker)
                    request = worker.submit("synthetic interrupt input")
                    self.assertTrue(hook_called.wait(5))
                    self.assertTrue(worker.join(5))
                self.assertTrue(cleaned.is_set())
                self.assertEqual(len(forwarded), 1)
                safe_error, original = forwarded[0], originals[0]
                self.assertIs(type(safe_error), interrupt_type)
                self.assertIsNot(safe_error, original)
                self.assertEqual(safe_error.args, ())
                self.assertIsNone(safe_error.__cause__)
                self.assertIsNone(safe_error.__context__)
                self.assertFalse(hasattr(safe_error, "__notes__"))
                self.assertNotIn(marker, stderr.getvalue())
                self.assertNotIn(marker, "".join(formatted))
                self.assertEqual(original.args, (marker,))
                self.assertEqual(original.__notes__, [marker + " note"])
                self.assertEqual(original.__cause__.args, (marker + " cause",))
                self.assertEqual(original.__context__.args, (marker + " context",))
                events = worker.poll_events()
                self.assertEqual([event.kind for event in events], ["fatal", "closed"])
                self.assertEqual(events[0].input_id, request.input_id)
                self.assertEqual([event.error_code for event in events],
                                 ["worker_interrupted", "worker_interrupted"])
                self.assertEqual(worker.state, "fatal")
                self.assertFalse(worker.can_retry)

    def test_sqlite_factory_operations_and_close_share_worker_thread(self):
        calls = []
        model = FakeModel([ModelError("synthetic retry"), "synthetic committed"])
        with tempfile.TemporaryDirectory(prefix="genesis-chat-worker-synthetic-") as folder:
            root = Path(folder) / "data"

            @contextmanager
            def store_factory():
                calls.append(("open", threading.get_ident()))
                with SQLiteStore(root) as store:
                    try:
                        yield store
                    finally:
                        # Querying SQLite here enforces its real thread affinity.
                        calls.append(("inspect", threading.get_ident()))
                        count = store._connection.execute("SELECT count(*) FROM sessions").fetchone()[0]
                        self.assertEqual(count, 1)
                calls.append(("closed", threading.get_ident()))

            worker, _ = self.worker(model, store_factory=store_factory)
            user_input = worker.submit("synthetic durable retry")
            self.assertEqual(self.receive(worker)[0].kind, "failed")
            self.assertEqual(worker.retry(), user_input)
            successful = self.receive(worker)[0]
            worker.close()
            self.assertTrue(worker.join(5))
            self.assertEqual([e.kind for e in worker.poll_events()], ["closed"])
            self.assertEqual([c[0] for c in calls], ["open", "inspect", "closed"])
            self.assertEqual(len({c[1] for c in calls}), 1)
            self.assertNotEqual(calls[0][1], threading.get_ident())
            # Reopen only after the owning worker closed the original connection.
            with SQLiteStore(root) as store:
                session_id = model.requests[-1].session_id
                snapshot = store.get_session(session_id)
                self.assertEqual(len(snapshot.turns), 1)
                self.assertEqual(snapshot.turns[0], successful.turn)


if __name__ == "__main__":
    unittest.main()
