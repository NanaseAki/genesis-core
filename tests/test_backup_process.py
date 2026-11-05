"""Spawned backup/recovery checks using only synthetic temporary ledgers."""

from contextlib import closing, contextmanager, nullcontext
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import genesis
from genesis.adapters.desktop_worker import ChatWorker
from genesis.core import Turn, UserInput
from genesis.models.contracts import Message, Role
from genesis.models.fake import FakeModel
from genesis.storage.sqlite import SQLiteStore


TIMEOUT = 20
DATABASE = "genesis-local-preview.sqlite3"
LOCK_NAME = ".genesis-initialization.lock"
TEXT = "  synthetic backup original\nexact input  "
REPLY = "synthetic backup reply"


def _metadata(root, session_id):
    with SQLiteStore(root, data_kind="local_preview", mode="open") as store:
        recovery = store.recovery_snapshot(session_id)
        inputs = [turn.user_input for turn in recovery.session.turns]
        inputs.extend(item.user_input for item in recovery.unfinished_requests)
        return {"identity": store.identity, "day0": store.day0(),
                "sessions": store.session_ids(), "recovery": recovery,
                "events": store.events(session_id),
                "attempts": {item.input_id: store.attempts(session_id, item.input_id)
                             for item in inputs}}


def _started(channel):
    channel.send({"phase": "started", "pid": os.getpid(), "module": genesis.__file__})


def _checkpoint(channel, point):
    channel.send({"phase": "checkpoint", "point": point})
    if not channel.poll(TIMEOUT):
        raise AssertionError("synthetic backup checkpoint timed out")
    command = channel.recv()
    if command == "crash":
        os._exit(73)
    if command == "interrupt":
        raise KeyboardInterrupt("synthetic backup interruption")
    if command != "continue":
        raise AssertionError("unexpected synthetic backup command")


def _operation(source, target, action, stop_at, channel):
    from genesis.storage import backup

    _started(channel)
    target = Path(target)
    original_mkdir = Path.mkdir
    original_open = Path.open
    original_manifest = backup._write_manifest
    original_publish = backup._publish_restore

    def observed_mkdir(path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if stop_at == "reserved" and path == target:
            _checkpoint(channel, "reserved")
        return result

    def observed_open(path, mode="r", *args, **kwargs):
        if stop_at == "before_snapshot" and path == target / "snapshot.sqlite3" and mode == "xb":
            _checkpoint(channel, "before_snapshot")
        return original_open(path, mode, *args, **kwargs)

    def observed_manifest(*args, **kwargs):
        if stop_at == "before_manifest":
            _checkpoint(channel, "before_manifest")
        return original_manifest(*args, **kwargs)

    def observed_publish(*args, **kwargs):
        if stop_at == "before_publish":
            _checkpoint(channel, "before_publish")
        result = original_publish(*args, **kwargs)
        if stop_at == "after_publish_error":
            raise OSError("synthetic failure after atomic publication")
        return result

    try:
        operation = backup.create_backup if action == "backup" else backup.restore_backup
        with patch.object(Path, "mkdir", observed_mkdir), \
                patch.object(Path, "open", observed_open), \
                patch.object(backup, "_write_manifest", observed_manifest), \
                patch.object(backup, "_publish_restore", observed_publish):
            operation(source, target)
        channel.send({"phase": "complete"})
    except BaseException as error:
        channel.send({"phase": "rejected", "error_type": type(error).__name__,
                      "reason": error.reason.value if isinstance(error, backup.BackupError) else None})
    finally:
        channel.close()


def _writer(root, attempt, channel):
    _started(channel)
    try:
        with SQLiteStore(root, data_kind="local_preview", mode="open") as store:
            original_commit = store._commit

            def before_commit():
                _checkpoint(channel, "uncommitted_turn")
                original_commit()

            store._commit = before_commit
            turn = Turn(attempt.user_input, Message(Role.ASSISTANT, REPLY), "fake-v1")
            committed = store.commit_turn(attempt, turn)
        channel.send({"phase": "complete", "turn": committed})
    except BaseException as error:
        channel.send({"phase": "rejected", "error_type": type(error).__name__})
    finally:
        channel.close()


def _copy_with_commit(source, target, channel):
    from genesis.storage.backup import BackupError, create_backup

    _started(channel)
    original_connect = sqlite3.connect
    observations = []
    paused = False

    class ObservedConnection(sqlite3.Connection):
        def backup(self, target, *, pages=-1, progress=None, name="main", sleep=0.250):
            def observed_progress(status, remaining, total):
                nonlocal paused
                observations.append((status, remaining, total))
                if status == sqlite3.SQLITE_OK and not paused:
                    paused = True
                    channel.send({"phase": "checkpoint", "point": "copy_in_progress",
                                  "remaining": remaining, "total": total, "pages": pages})
                    if not channel.poll(TIMEOUT) or channel.recv() != "committed":
                        raise AssertionError("synthetic writer did not confirm commit")
                if progress is not None:
                    progress(status, remaining, total)

            # Real native backup steps run both before and after the writer.
            return super().backup(target, pages=pages, progress=observed_progress,
                                  name=name, sleep=sleep)

    def observed_connect(*args, **kwargs):
        kwargs["factory"] = ObservedConnection
        return original_connect(*args, **kwargs)

    try:
        with patch.object(sqlite3, "connect", observed_connect):
            create_backup(source, target)
        channel.send({"phase": "complete", "progress": observations})
    except BaseException as error:
        channel.send({"phase": "rejected", "error_type": type(error).__name__,
                      "reason": error.reason.value if isinstance(error, BackupError) else None})
    finally:
        channel.close()


def _profile_creator(root, data_kind, channel):
    _started(channel)
    try:
        with SQLiteStore(root, data_kind=data_kind, mode="create"):
            pass
        channel.send({"phase": "complete"})
    except BaseException as error:
        channel.send({"phase": "rejected", "error_type": type(error).__name__})
    finally:
        channel.close()


def _reopen(root, session_id, channel):
    _started(channel)
    worker = None
    model = FakeModel([])

    @contextmanager
    def store_factory():
        with SQLiteStore(root, data_kind="local_preview", mode="open") as store:
            yield store

    try:
        before = _metadata(root, session_id)
        worker = ChatWorker(lambda: nullcontext(model), store_factory=store_factory,
                            startup="resume", resume_session_id=session_id)
        if not worker.wait_for_events(5):
            raise AssertionError("synthetic restored worker startup timed out")
        events = worker.poll_events()
        if len(events) != 1 or events[0].kind != "ready":
            raise AssertionError("restored worker did not publish ready")
        ready = events[0]
        worker.close()
        if not worker.join(6):
            raise AssertionError("synthetic restored worker did not close")
        if [event.kind for event in worker.poll_events()] != ["closed"]:
            raise AssertionError("restored worker did not publish closed")
        channel.send({"phase": "complete", "before": before,
                      "after": _metadata(root, session_id), "ready": ready,
                      "model_calls": len(model.requests)})
    except BaseException as error:
        channel.send({"phase": "rejected", "error_type": type(error).__name__})
    finally:
        if worker is not None:
            worker.close()
            if not worker.join(6):
                raise AssertionError("synthetic restored worker cleanup timed out")
        channel.close()


class BackupProcessTests(unittest.TestCase):
    def receive(self, channel, phase):
        self.assertTrue(channel.poll(TIMEOUT), "no synthetic backup process message")
        result = channel.recv()
        self.assertEqual(result["phase"], phase, result)
        return result

    @contextmanager
    def child(self, target, *args):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(target=target, args=(*args, child))
        try:
            try:
                process.start()
            finally:
                child.close()
            started = self.receive(parent, "started")
            self.assertNotEqual(started["pid"], os.getpid())
            self.assertEqual(Path(started["module"]).resolve(), Path(genesis.__file__).resolve())
            yield process, parent
        finally:
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join(5)
                self.assertFalse(process.is_alive(), "synthetic backup child cleanup failed")
                process.close()
            parent.close()

    def finished(self, process, exit_code=0):
        process.join(TIMEOUT)
        self.assertFalse(process.is_alive(), "synthetic backup child did not exit")
        self.assertEqual(process.exitcode, exit_code)

    def operation(self, source, target, action, *, rejected=False, stop_at=None):
        with self.child(_operation, str(source), str(target), action, stop_at) as (process, channel):
            result = self.receive(channel, "rejected" if rejected else "complete")
            self.finished(process)
            return result

    def seed(self, root, *, padding=""):
        session_id = uuid4()
        committed = UserInput(uuid4(), "synthetic initial committed input")
        pending = UserInput(uuid4(), TEXT)
        with SQLiteStore(root, data_kind="local_preview", mode="create") as store:
            store.create_session(session_id)
            attempt = store.begin_attempt(session_id, committed)
            store.commit_turn(attempt, Turn(committed, Message(Role.ASSISTANT,
                              "synthetic initial reply" + padding), "fake-v1"))
            pending_attempt = store.begin_attempt(session_id, pending)
        return session_id, pending_attempt

    def files(self, root):
        return {path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*") if path.is_file()}

    def test_backup_during_uncommitted_writer_preserves_complete_pending_then_committed(self):
        from genesis.storage.backup import restore_backup, verify_backup

        with tempfile.TemporaryDirectory(prefix="genesis-backup-process-synthetic-") as folder:
            root = Path(folder)
            source = root / "source"
            session_id, attempt = self.seed(source)
            before = _metadata(source, session_id)
            with self.child(_writer, str(source), attempt) as (writer, channel):
                self.assertEqual(self.receive(channel, "checkpoint")["point"], "uncommitted_turn")
                # The real writer has applied its complete SQL mutation but
                # has not committed. A reader must obtain the prior ledger.
                self.operation(source, root / "pending-backup", "backup")
                verify_backup(root / "pending-backup")
                restore_backup(root / "pending-backup", root / "pending-restored")
                self.assertEqual(_metadata(root / "pending-restored", session_id), before)
                channel.send("continue")
                result = self.receive(channel, "complete")
                self.finished(writer)
            after = _metadata(source, session_id)
            self.assertEqual(after["recovery"].session.turns[-1], result["turn"])
            self.assertEqual(after["recovery"].unfinished_requests, ())
            self.assertEqual([item.state for item in after["attempts"][attempt.user_input.input_id]],
                             ["committed"])
            self.operation(source, root / "committed-backup", "backup")
            verify_backup(root / "committed-backup")
            restore_backup(root / "committed-backup", root / "committed-restored")
            self.assertEqual(_metadata(root / "committed-restored", session_id), after)

    def test_native_incremental_backup_restarts_after_real_writer_commit(self):
        from genesis.storage.backup import restore_backup, verify_backup

        with tempfile.TemporaryDirectory(prefix="genesis-backup-restart-copy-synthetic-") as folder:
            root = Path(folder)
            source = root / "source"
            session_id, attempt = self.seed(source, padding="synthetic page content\n" * 40000)
            # Keep a real WAL connection open so the writer can commit while
            # the online backup is paused between native 128-page steps.
            with closing(sqlite3.connect(source / DATABASE, isolation_level=None)) as anchor:
                self.assertEqual(anchor.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
                self.assertGreater(anchor.execute("PRAGMA page_count").fetchone()[0], 128)
                with self.child(_copy_with_commit, str(source), str(root / "backup")) as (copier, channel):
                    checkpoint = self.receive(channel, "checkpoint")
                    self.assertEqual(checkpoint["point"], "copy_in_progress")
                    self.assertEqual(checkpoint["pages"], 128)
                    self.assertGreater(checkpoint["total"], 128)
                    self.assertGreater(checkpoint["remaining"], 0)
                    with self.child(_writer, str(source), attempt) as (writer, other):
                        self.assertEqual(self.receive(other, "checkpoint")["point"], "uncommitted_turn")
                        other.send("continue")
                        committed = self.receive(other, "complete")
                        self.finished(writer)
                    expected = _metadata(source, session_id)
                    self.assertEqual(expected["recovery"].session.turns[-1], committed["turn"])
                    self.assertEqual(expected["recovery"].unfinished_requests, ())
                    channel.send("committed")
                    copied = self.receive(channel, "complete")
                    self.finished(copier)
                self.assertEqual(copied["progress"][0][0], sqlite3.SQLITE_OK)
                self.assertEqual(copied["progress"][-1][0], sqlite3.SQLITE_DONE)
                self.assertGreater(len(copied["progress"]), 1)
                report = verify_backup(root / "backup")
                self.assertEqual((report.turn_count, report.unfinished_request_count), (2, 0))
                restore_backup(root / "backup", root / "restored")
                self.assertEqual(_metadata(root / "restored", session_id), expected)

    def contend(self, action):
        from genesis.storage.backup import create_backup, verify_backup

        with tempfile.TemporaryDirectory(prefix="genesis-backup-contend-synthetic-") as folder:
            root = Path(folder)
            source = root / "source"
            session_id, _ = self.seed(source)
            expected = _metadata(source, session_id)
            if action == "restore":
                create_backup(source, root / "backup")
                source = root / "backup"
            target = root / "contended"
            with self.child(_operation, str(source), str(target), action, "reserved") as (winner, channel):
                self.assertEqual(self.receive(channel, "checkpoint")["point"], "reserved")
                self.assertTrue(target.is_dir())
                reserved = self.files(target)
                rejected = self.operation(source, target, action, rejected=True)
                self.assertEqual(rejected["reason"], "target_exists")
                self.assertEqual(self.files(target), reserved)
                channel.send("continue")
                self.receive(channel, "complete")
                self.finished(winner)
            if action == "backup":
                verify_backup(target)
                self.assertEqual(set(self.files(target)), {"snapshot.sqlite3", "manifest.json", LOCK_NAME})
                self.assertEqual((target / LOCK_NAME).read_bytes(), b"")
            else:
                self.assertEqual(_metadata(target, session_id), expected)
            completed = self.files(target)
            rejected = self.operation(source, target, action, rejected=True)
            self.assertEqual(rejected["reason"], "target_exists")
            self.assertEqual(self.files(target), completed)

    def test_backup_same_target_process_race_never_overwrites_winner(self):
        self.contend("backup")

    def test_restore_same_target_process_race_never_overwrites_winner(self):
        self.contend("restore")

    def test_backup_and_profile_initializers_arbitrate_both_winner_orders(self):
        from genesis.storage.backup import verify_backup

        kinds = ("synthetic_development", "local_preview")
        for kind in kinds:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(
                    prefix="genesis-backup-profile-synthetic-") as folder:
                root = Path(folder)
                source = root / "source"
                self.seed(source)
                original = self.files(source)
                artifact = root / "backup-first"
                with self.child(_operation, str(source), str(artifact), "backup",
                                "before_snapshot") as (winner, channel):
                    self.assertEqual(self.receive(channel, "checkpoint")["point"], "before_snapshot")
                    self.assertEqual({entry.name for entry in artifact.iterdir()}, {LOCK_NAME})
                    self.assertEqual((artifact / LOCK_NAME).stat().st_size, 0)
                    with self.child(_profile_creator, str(artifact), kind) as (other, messages):
                        rejected = self.receive(messages, "rejected")
                        self.assertEqual(rejected["error_type"], "InitializationBusyError")
                        self.finished(other)
                    self.assertEqual({entry.name for entry in artifact.iterdir()}, {LOCK_NAME})
                    channel.send("continue")
                    self.receive(channel, "complete")
                    self.finished(winner)
                verify_backup(artifact)
                completed = self.files(artifact)
                self.assertEqual(set(completed), {"snapshot.sqlite3", "manifest.json", LOCK_NAME})
                self.assertEqual(completed[LOCK_NAME], b"")
                for later_kind in kinds:
                    with self.child(_profile_creator, str(artifact), later_kind) as (other, messages):
                        rejected = self.receive(messages, "rejected")
                        self.assertEqual(rejected["error_type"], "ValueError")
                        self.finished(other)
                    self.assertEqual(self.files(artifact), completed)

                selected = root / "profile-first"
                with self.child(_operation, str(source), str(selected), "backup",
                                "reserved") as (loser, channel):
                    self.assertEqual(self.receive(channel, "checkpoint")["point"], "reserved")
                    self.assertEqual(tuple(selected.iterdir()), ())
                    with self.child(_profile_creator, str(selected), kind) as (winner, messages):
                        self.receive(messages, "complete")
                        self.finished(winner)
                    retained = self.files(selected)
                    expected_name = ("genesis-synthetic.sqlite3" if kind == "synthetic_development"
                                     else DATABASE)
                    self.assertEqual(set(retained), {expected_name, LOCK_NAME})
                    channel.send("continue")
                    rejected = self.receive(channel, "rejected")
                    self.assertEqual(rejected["reason"], "target_exists")
                    self.finished(loser)
                self.assertEqual(self.files(selected), retained)
                self.assertFalse((selected / "snapshot.sqlite3").exists())
                self.assertFalse((selected / "manifest.json").exists())
                self.assertEqual(self.files(source), original)

    def test_restored_new_process_preserves_identity_history_pending_without_generation(self):
        from genesis.storage.backup import create_backup, restore_backup

        with tempfile.TemporaryDirectory(prefix="genesis-backup-restart-synthetic-") as folder:
            root = Path(folder)
            source = root / "source"
            session_id, attempt = self.seed(source)
            expected = _metadata(source, session_id)
            original = self.files(source)
            create_backup(source, root / "backup")
            restore_backup(root / "backup", root / "restored")
            with self.child(_reopen, str(root / "restored"), session_id) as (process, channel):
                result = self.receive(channel, "complete")
                self.finished(process)
            self.assertEqual(result["model_calls"], 0)
            self.assertEqual(result["before"], expected)
            self.assertEqual(result["after"], expected)
            self.assertEqual(result["ready"].session_id, session_id)
            self.assertEqual(result["ready"].history, expected["recovery"].session.turns)
            self.assertEqual(result["ready"].pending_requests, expected["recovery"].unfinished_requests)
            self.assertEqual(result["ready"].pending_requests[0].user_input, attempt.user_input)
            self.assertEqual(self.files(source), original)

    def test_interruption_and_process_exit_before_publication_leave_no_success(self):
        from genesis.storage.backup import BackupError, create_backup, verify_backup

        for action, point in (("backup", "before_manifest"), ("restore", "before_publish")):
            for command in ("interrupt", "crash"):
                with self.subTest(action=action, command=command), tempfile.TemporaryDirectory(
                        prefix="genesis-backup-failure-synthetic-") as folder:
                    root = Path(folder)
                    source = root / "source"
                    session_id, _ = self.seed(source)
                    expected = _metadata(source, session_id)
                    original = self.files(source)
                    if action == "restore":
                        create_backup(source, root / "backup")
                        operation_source = root / "backup"
                    else:
                        operation_source = source
                    input_files = self.files(operation_source)
                    target = root / "interrupted"
                    with self.child(_operation, str(operation_source), str(target),
                                    action, point) as (process, channel):
                        self.assertEqual(self.receive(channel, "checkpoint")["point"], point)
                        if action == "backup":
                            self.assertGreater((target / "snapshot.sqlite3").stat().st_size, 0)
                            self.assertFalse((target / "manifest.json").exists())
                        else:
                            self.assertGreater((target / ".genesis-restore-staging.sqlite3").stat().st_size, 0)
                            self.assertFalse((target / DATABASE).exists())
                            # A populated restore stage is a reserved name,
                            # even before a live profile has been published.
                            with self.child(_profile_creator, str(target), "synthetic_development") as (competitor, other):
                                rejected = self.receive(other, "rejected")
                                self.assertEqual(rejected["error_type"], "ValueError")
                                self.finished(competitor)
                            self.assertFalse((target / "genesis-synthetic.sqlite3").exists())
                        channel.send(command)
                        if command == "interrupt":
                            result = self.receive(channel, "rejected")
                            self.assertEqual(result["error_type"], "KeyboardInterrupt")
                            self.finished(process)
                        else:
                            self.finished(process, 73)
                            # A process exit cannot send a successful API receipt.
                            # Windows can report a broken named pipe instead
                            # of EOF after os._exit closes its last endpoint.
                            with self.assertRaises((EOFError, BrokenPipeError)):
                                channel.recv()
                    if action == "backup":
                        with self.assertRaises(BackupError):
                            verify_backup(target)
                    else:
                        self.assertFalse((target / DATABASE).exists())
                        with self.assertRaises(FileNotFoundError):
                            SQLiteStore(target, data_kind="local_preview", mode="open")
                    remaining = self.files(target)
                    rejected = self.operation(operation_source, target, action, rejected=True)
                    self.assertEqual(rejected["reason"], "target_exists")
                    self.assertEqual(self.files(target), remaining)
                    self.assertEqual(self.files(operation_source), input_files)
                    self.assertEqual(self.files(source), original)
                    self.assertEqual(_metadata(source, session_id), expected)

    def test_restore_crash_staging_blocks_both_profile_initializers_without_changes(self):
        from genesis.storage.backup import create_backup

        with tempfile.TemporaryDirectory(prefix="genesis-restore-stage-crash-synthetic-") as folder:
            root = Path(folder)
            source, artifact, target = root / "source", root / "backup", root / "interrupted"
            self.seed(source)
            original = self.files(source)
            create_backup(source, artifact)
            backup_files = self.files(artifact)
            staging = target / ".genesis-restore-staging.sqlite3"
            with self.child(_operation, str(artifact), str(target), "restore",
                            "before_publish") as (process, channel):
                self.assertEqual(self.receive(channel, "checkpoint")["point"], "before_publish")
                self.assertEqual({entry.name for entry in target.iterdir()}, {LOCK_NAME, staging.name})
                self.assertEqual(staging.read_bytes(), backup_files["snapshot.sqlite3"])
                # This real process exits with the fully validated private
                # stage still present, without running Python cleanup code.
                channel.send("crash")
                self.finished(process, 73)
                with self.assertRaises((EOFError, BrokenPipeError)):
                    channel.recv()
            retained = self.files(target)
            self.assertEqual(set(retained), {LOCK_NAME, staging.name})
            self.assertEqual(retained[LOCK_NAME], b"")
            for kind in ("synthetic_development", "local_preview"):
                with self.subTest(kind=kind):
                    with self.child(_profile_creator, str(target), kind) as (process, channel):
                        rejected = self.receive(channel, "rejected")
                        self.assertEqual(rejected["error_type"], "ValueError")
                        self.finished(process)
                    self.assertFalse((target / "genesis-synthetic.sqlite3").exists())
                    self.assertFalse((target / DATABASE).exists())
                    self.assertEqual(self.files(target), retained)
            # The compatible default synthetic entry point is an initializer
            # too; a retained stage must not be mistaken for a new data root.
            with self.assertRaises(ValueError):
                SQLiteStore(target)
            self.assertEqual(self.files(target), retained)
            self.assertEqual(self.files(source), original)
            self.assertEqual(self.files(artifact), backup_files)

    def test_error_after_restore_publication_reports_unknown_and_preserves_ledger(self):
        from genesis.storage.backup import create_backup

        with tempfile.TemporaryDirectory(prefix="genesis-backup-published-synthetic-") as folder:
            root = Path(folder)
            source = root / "source"
            session_id, _ = self.seed(source)
            expected = _metadata(source, session_id)
            create_backup(source, root / "backup")
            original = self.files(root / "backup")
            target = root / "restored"
            result = self.operation(root / "backup", target, "restore", rejected=True,
                                    stop_at="after_publish_error")
            self.assertEqual(result["reason"], "outcome_unknown")
            # Publication may have succeeded despite the lost return. Keep the
            # complete ledger for explicit inspection; never retry over it.
            self.assertEqual(_metadata(target, session_id), expected)
            retained = self.files(target)
            retry = self.operation(root / "backup", target, "restore", rejected=True)
            self.assertEqual(retry["reason"], "target_exists")
            self.assertEqual(self.files(target), retained)
            self.assertEqual(self.files(root / "backup"), original)


if __name__ == "__main__":
    unittest.main()
