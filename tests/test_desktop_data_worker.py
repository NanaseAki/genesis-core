"""Synthetic data tasks without native windows or real model calls."""

from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import threading
import traceback
import unittest
from unittest.mock import patch
from uuid import UUID

from genesis.adapters.desktop_data_worker import DataTaskRequest, DataTaskWorker
from genesis.storage.backup import BackupError, BackupFailure, BackupReport, RestoreReport
from genesis.storage.sqlite import SQLiteStore


class DataTaskWorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="genesis-data-task-synthetic-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def request(self, operation="verify"):
        return DataTaskRequest(operation, str(self.root / "source"),
            None if operation == "verify" else str(self.root / "destination"), operation != "verify")

    def report(self):
        return BackupReport(self.root / "backup", UUID(int=5801), "1" * 64,
                            2, 3, 4, "2" * 64, "2026-11-05T00:00:00Z")

    def finish(self, worker):
        worker.close()
        self.assertTrue(worker.join(5), "synthetic data worker did not finish")

    def run_worker(self, runner, operation="verify"):
        worker = DataTaskWorker(self.request(operation), operation_runner=runner)
        self.addCleanup(self.finish, worker)
        self.assertTrue(worker.join(5))
        return worker, worker.poll_events()

    def test_request_validation_freezes_values_without_filesystem_or_thread_work(self):
        invalid = (
            {"operation": "remove", "source_root": str(self.root)},
            {"operation": "verify", "source_root": "relative"},
            {"operation": "verify", "source_root": str(self.root) + "\0"},
            {"operation": "verify", "source_root": str(self.root) + "\ud800"},
            {"operation": "verify", "source_root": self.root},
            {"operation": "verify", "source_root": str(self.root), "destination_root": str(self.root / "new")},
            {"operation": "backup", "source_root": str(self.root), "destination_root": str(self.root / "new")},
            {"operation": "restore", "source_root": str(self.root), "plaintext_acknowledged": True},
            {"operation": "backup", "source_root": str(self.root), "destination_root": "relative", "plaintext_acknowledged": True},
            {"operation": "verify", "source_root": str(self.root), "plaintext_acknowledged": 1},
            {"operation": "verify", "source_root": str(self.root), "operation_id": "synthetic-id"},
        )
        with patch.object(Path, "resolve", side_effect=AssertionError("filesystem inspection")), patch.object(
                Path, "exists", side_effect=AssertionError("filesystem inspection")), patch.object(
                threading.Thread, "start", side_effect=AssertionError("unexpected native thread")):
            for kwargs in invalid:
                with self.subTest(kwargs=repr(kwargs)), self.assertRaises((TypeError, ValueError)):
                    DataTaskRequest(**kwargs)
            request = self.request("backup")
            with self.assertRaises(FrozenInstanceError):
                request.source_root = "changed"
            with self.assertRaises(TypeError):
                DataTaskWorker(object())
            with self.assertRaises(TypeError):
                DataTaskWorker(request, operation_runner=42)

    def test_default_dispatch_runs_once_on_owner_thread_and_publishes_immutable_reports(self):
        for operation, function in (("backup", "create_backup"), ("verify", "verify_backup"), ("restore", "restore_backup")):
            with self.subTest(operation=operation):
                request, backup, calls = self.request(operation), self.report(), []
                report = RestoreReport(self.root / "restored", backup) if operation == "restore" else backup

                def perform(*args):
                    calls.append((args, threading.get_ident()))
                    return report

                with patch("genesis.adapters.desktop_data_worker." + function, side_effect=perform):
                    worker = DataTaskWorker(request)
                    self.addCleanup(self.finish, worker)
                    self.assertFalse(worker._thread.daemon)
                    self.assertTrue(worker.join(5))
                self.assertEqual(len(calls), 1)
                self.assertNotEqual(calls[0][1], threading.get_ident())
                self.assertEqual(calls[0][0], (request.source_root,) if operation == "verify" else
                                 (request.source_root, request.destination_root))
                self.assertIs(worker.request, request)
                with self.assertRaises(AttributeError):
                    worker.request = self.request()
                self.assertTrue(worker.wait_for_events(0))
                events = worker.poll_events()
                self.assertEqual([event.kind for event in events], ["succeeded", "closed"])
                self.assertTrue(all(event.operation_id == request.operation_id and event.operation == operation for event in events))
                self.assertIs(events[0].report, report)
                self.assertIsNone(events[1].report)
                with self.assertRaises(FrozenInstanceError):
                    events[0].error_code = "changed"
                self.assertEqual(worker.state, "closed")
                self.assertEqual(worker.poll_events(), ())
                self.assertFalse(worker.wait_for_events(0))

    def test_close_during_work_returns_immediately_and_preserves_result_after_same_thread_cleanup(self):
        entered, release = threading.Event(), threading.Event()
        ownership = []
        report = self.report()

        @contextmanager
        def resource():
            ownership.append(("enter", threading.get_ident()))
            try:
                yield
            finally:
                ownership.append(("exit", threading.get_ident()))

        def run(request):
            with resource():
                ownership.append(("run", threading.get_ident()))
                entered.set()
                if not release.wait(5):
                    raise AssertionError("synthetic release missing")
                return report

        worker = DataTaskWorker(self.request(), operation_runner=run)
        try:
            self.assertTrue(entered.wait(5))
            with patch.object(threading.Thread, "join", side_effect=AssertionError("close must not join")):
                worker.close()
                worker.close()
            self.assertEqual(worker.state, "closing")
            self.assertTrue(worker.is_alive)
            self.assertEqual(worker.poll_events(), ())
            self.assertEqual([action for action, _ in ownership], ["enter", "run"])
            release.set()
            self.assertTrue(worker.join(5))
            events = worker.poll_events()
            self.assertEqual([event.kind for event in events], ["succeeded", "closed"])
            self.assertIs(events[0].report, report)
            self.assertEqual([action for action, _ in ownership], ["enter", "run", "exit"])
            self.assertEqual(len({owner for _, owner in ownership}), 1)
            self.assertNotEqual(ownership[0][1], threading.get_ident())
        finally:
            release.set()
            self.finish(worker)

    def test_close_before_run_skips_operation_and_emits_only_closed(self):
        calls = []
        real_start = threading.Thread.start
        with patch.object(threading.Thread, "start", lambda thread: None):
            worker = DataTaskWorker(self.request(), operation_runner=lambda request: calls.append(request))
        worker.close()
        real_start(worker._thread)
        self.addCleanup(self.finish, worker)
        self.assertTrue(worker.join(5))
        self.assertEqual(calls, [])
        events = worker.poll_events()
        self.assertEqual([event.kind for event in events], ["closed"])
        self.assertIsNone(events[0].error_code)

    def test_expected_failures_publish_fixed_categories_without_payload_chain_or_automatic_retry(self):
        for reason in BackupFailure:
            with self.subTest(reason=reason.value):
                marker, calls = "SYNTHETIC_PRIVATE_DATA_ERROR", []

                def run(request):
                    calls.append(request)
                    failure = BackupError(reason)
                    failure.add_note(marker)
                    raise failure from ValueError(marker + " cause")

                worker, events = self.run_worker(run)
                self.assertEqual(len(calls), 1)
                self.assertEqual([event.kind for event in events], ["failed", "closed"])
                self.assertTrue(all(event.error_code == "data_" + reason.value for event in events))
                self.assertTrue(all(event.report is None for event in events))
                self.assertNotIn(marker, repr(events))

    def test_unexpected_failure_and_wrong_report_are_fatal_without_success_or_raw_payload(self):
        def broken(request):
            raise RuntimeError("SYNTHETIC_PRIVATE_PROGRAMMING_ERROR")

        for runner in (broken, lambda request: "SYNTHETIC_PRIVATE_REPORT", lambda request: RestoreReport(self.root, self.report())):
            with self.subTest(runner=runner):
                worker, events = self.run_worker(runner)
                self.assertEqual([event.kind for event in events], ["fatal", "closed"])
                self.assertTrue(all(event.error_code == "data_internal_error" for event in events))
                self.assertNotIn("SYNTHETIC_PRIVATE", repr(events))

    def test_builtin_interruptions_cleanup_then_propagate_sanitized_same_type(self):
        for error_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(error_type=error_type.__name__):
                marker, ownership, captured, originals = "SYNTHETIC_PRIVATE_INTERRUPTION", [], [], []

                @contextmanager
                def resource():
                    ownership.append(("enter", threading.get_ident()))
                    try:
                        yield
                    finally:
                        ownership.append(("exit", threading.get_ident()))

                def run(request):
                    with resource():
                        failure = error_type(marker)
                        failure.add_note(marker + " note")
                        originals.append(failure)
                        raise failure from ValueError(marker + " cause")

                def hook(args):
                    captured.append((args.exc_type, args.exc_value, "".join(traceback.format_exception(
                        args.exc_type, args.exc_value, args.exc_traceback))))

                with patch.object(threading, "excepthook", hook):
                    worker, events = self.run_worker(run)
                self.assertEqual([event.kind for event in events], ["fatal", "closed"])
                self.assertTrue(all(event.error_code == "data_interrupted" for event in events))
                self.assertEqual(len(captured), 1)
                self.assertIs(captured[0][0], error_type)
                self.assertIsNot(captured[0][1], originals[0])
                self.assertEqual(captured[0][1].args, ())
                self.assertIsNone(captured[0][1].__cause__)
                self.assertIsNone(captured[0][1].__context__)
                self.assertNotIn(marker, captured[0][2])
                self.assertNotIn(marker, repr(events))
                self.assertEqual(originals[0].args, (marker,))
                self.assertEqual([item[0] for item in ownership], ["enter", "exit"])
                self.assertEqual(ownership[0][1], ownership[1][1])

    def test_custom_base_exception_is_not_swallowed_but_events_remain_safe(self):
        class SyntheticInterrupt(BaseException):
            pass
        marker, captured = SyntheticInterrupt("synthetic custom interruption"), []

        def run(request):
            raise marker

        with patch.object(threading, "excepthook", lambda args: captured.append(args.exc_value)):
            worker, events = self.run_worker(run)
        self.assertEqual(captured, [marker])
        self.assertEqual([event.kind for event in events], ["fatal", "closed"])
        self.assertTrue(all(event.error_code == "data_interrupted" for event in events))
        self.assertNotIn(str(marker), repr(events))

    def test_constructor_pre_native_failure_preserves_original_and_requests_close_without_join(self):
        real_start = threading.Thread.start
        for error_type in (RuntimeError, KeyboardInterrupt, SystemExit):
            with self.subTest(error_type=error_type.__name__):
                marker, captured, calls = error_type("synthetic start failure"), [], []

                def fail(thread):
                    if thread.name != "genesis-data-task":
                        return real_start(thread)
                    captured.append(thread._target.__self__)
                    raise marker

                with patch.object(threading.Thread, "start", fail), patch.object(threading.Thread, "join",
                        side_effect=AssertionError("constructor must not join")):
                    with self.assertRaises(error_type) as caught:
                        DataTaskWorker(self.request(), operation_runner=lambda request: calls.append(request))
                self.assertIs(caught.exception, marker)
                self.assertEqual(len(captured), 1)
                self.assertEqual(captured[0].state, "closing")
                self.assertFalse(captured[0].is_alive)
                self.assertEqual(calls, [])

    def test_constructor_native_start_failure_closes_owner_without_cancelling_active_operation(self):
        real_start = threading.Thread.start
        for error_type in (RuntimeError, KeyboardInterrupt, SystemExit):
            with self.subTest(error_type=error_type.__name__):
                entered, release = threading.Event(), threading.Event()
                marker, captured, ownership = error_type("synthetic native wait failure"), [], []
                report = self.report()

                def run(request):
                    ownership.append(("enter", threading.get_ident()))
                    entered.set()
                    try:
                        if not release.wait(5):
                            raise AssertionError("synthetic release missing")
                        return report
                    finally:
                        ownership.append(("exit", threading.get_ident()))

                def fail_after_start(thread):
                    if thread.name != "genesis-data-task":
                        return real_start(thread)
                    captured.append(thread._target.__self__)
                    real_start(thread)
                    if not entered.wait(5):
                        raise AssertionError("synthetic runner did not start")
                    raise marker

                try:
                    with patch.object(threading.Thread, "start", fail_after_start), patch.object(
                            threading.Thread, "join", side_effect=AssertionError("constructor must not join")):
                        with self.assertRaises(error_type) as caught:
                            DataTaskWorker(self.request(), operation_runner=run)
                    self.assertIs(caught.exception, marker)
                    self.assertEqual(captured[0].state, "closing")
                    self.assertTrue(captured[0].is_alive)
                    release.set()
                    self.assertTrue(captured[0].join(5))
                    self.assertEqual([event.kind for event in captured[0].poll_events()], ["succeeded", "closed"])
                    self.assertEqual([action for action, _ in ownership], ["enter", "exit"])
                    self.assertEqual(ownership[0][1], ownership[1][1])
                finally:
                    release.set()
                    for worker in captured:
                        self.finish(worker)

    def test_constructor_interrupt_before_started_signal_closes_late_native_thread(self):
        real_start, real_bootstrap = threading.Thread.start, threading.Thread._bootstrap_inner
        bootstrap_entered, release_bootstrap = threading.Event(), threading.Event()
        marker, captured, calls, timeouts = KeyboardInterrupt("synthetic early interruption"), [], [], []

        def bootstrap(thread):
            if thread.name == "genesis-data-task":
                bootstrap_entered.set()
                if not release_bootstrap.wait(5):
                    timeouts.append(True)
            return real_bootstrap(thread)

        def fail_wait(thread):
            if thread.name != "genesis-data-task":
                return real_start(thread)
            captured.append(thread._target.__self__)
            real_wait = thread._started.wait

            def interrupted_wait(*args, **kwargs):
                if not bootstrap_entered.wait(5):
                    raise AssertionError("synthetic bootstrap missing")
                self.assertFalse(thread._started.is_set())
                raise marker

            thread._started.wait = interrupted_wait
            try:
                return real_start(thread)
            finally:
                thread._started.wait = real_wait

        try:
            with patch.object(threading.Thread, "_bootstrap_inner", bootstrap), patch.object(
                    threading.Thread, "start", fail_wait), patch.object(threading.Thread, "join",
                    side_effect=AssertionError("constructor must not join")):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    DataTaskWorker(self.request(), operation_runner=lambda request: calls.append(request))
                self.assertIs(caught.exception, marker)
                self.assertEqual(captured[0].state, "closing")
                self.assertFalse(captured[0]._thread._started.is_set())
                release_bootstrap.set()
            self.assertTrue(captured[0]._thread._started.wait(5))
            self.assertTrue(captured[0].join(5), "late data worker was not released")
            self.assertEqual(calls, [])
            self.assertEqual(timeouts, [])
            self.assertEqual([event.kind for event in captured[0].poll_events()], ["closed"])
        finally:
            release_bootstrap.set()
            for worker in captured:
                self.assertTrue(worker._thread._started.wait(5))
                self.finish(worker)

    def test_real_synthetic_backup_verify_restore_runs_through_default_dispatch(self):
        source, backup, destination = self.root / "source", self.root / "backup", self.root / "restored"
        with SQLiteStore(source, data_kind="local_preview", mode="create") as store:
            original_identity = store.identity.companion_id
            original_day0 = store.day0()
            store.create_session(UUID(int=5802))
        requests = (
            DataTaskRequest("backup", str(source), str(backup), True),
            DataTaskRequest("verify", str(backup)),
            DataTaskRequest("restore", str(backup), str(destination), True),
        )
        for request in requests:
            with self.subTest(operation=request.operation):
                worker = DataTaskWorker(request)
                self.addCleanup(self.finish, worker)
                self.assertTrue(worker.join(5))
                events = worker.poll_events()
                self.assertEqual([event.kind for event in events], ["succeeded", "closed"])
                report = events[0].report.backup if request.operation == "restore" else events[0].report
                self.assertEqual(report.companion_id, original_identity)
                self.assertEqual((report.session_count, report.turn_count, report.unfinished_request_count), (1, 0, 0))
        with SQLiteStore(destination, data_kind="local_preview", mode="open") as store:
            self.assertEqual(store.identity.companion_id, original_identity)
            self.assertEqual(store.day0(), original_day0)
            self.assertEqual(store.session_ids(), (UUID(int=5802),))


if __name__ == "__main__":
    unittest.main()
