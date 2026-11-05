"""Plaintext local-preview backups; all data below is temporary synthetic text."""

from contextlib import closing
from dataclasses import FrozenInstanceError
import hashlib
import io
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import threading
import traceback
import unittest
from unittest.mock import patch
from uuid import UUID

from genesis.core import GenesisCore, UserInput
from genesis.models.fake import FakeModel
from genesis.storage import backup
from genesis.storage.backup import BackupError, BackupFailure, create_backup, restore_backup, verify_backup
from genesis.storage.sqlite import DataIntegrityError, SQLiteStore
from genesis.storage._initialization import InitializationBusyError, LOCK_FILENAME


def uid(value):
    return UUID(int=value)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "synthetic-preview"
        with SQLiteStore(self.source, data_kind="local_preview", mode="create") as store:
            self.identity, self.day0 = store.identity, store.day0()
            core = GenesisCore(store.identity, FakeModel(["synthetic reply"]), store=store)
            core.create_session(uid(1))
            core.send(uid(1), UserInput(uid(2), "  synthetic original\ninput  "))
            store.begin_attempt(uid(1), UserInput(uid(3), "synthetic pending input"))
            self.session = store.get_session(uid(1))
            self.pending = store.unfinished_requests(uid(1))
        self.database = self.source / backup.ACTIVE_FILENAME

    def rejected(self, reason, function, *args, **kwargs):
        with self.assertRaises(BackupError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(str(caught.exception), reason.value)
        return caught.exception

    def artifact(self, name="artifact"):
        destination = self.root / name
        return destination, create_backup(self.source, destination)

    def assert_ambient_private(self, operation, reason):
        marker = "SYNTHETIC_BACKUP_AMBIENT_PRIVATE_4839"
        try:
            raise ValueError(marker + " cause")
        except ValueError as cause:
            try:
                raise RuntimeError(marker + " caller") from cause
            except RuntimeError as ambient:
                ambient.add_note(marker + " note")
                before = (ambient.args, ambient.__cause__, ambient.__context__,
                          ambient.__traceback__, tuple(ambient.__notes__))
                try:
                    operation()
                except BackupError as error:
                    self.assertEqual(error.reason, reason)
                    self.assertEqual(error.args, (reason.value,))
                    self.assertEqual(error.__dict__, {"reason": reason})
                    self.assertIsNone(error.__cause__)
                    self.assertIsNone(error.__context__)
                    self.assertNotIn(marker, "".join(traceback.format_exception(error)))
                    # Exercise ordinary logging while the actual traceback is
                    # still attached; never inspect or capture traceback locals.
                    output = io.StringIO()
                    handler = logging.StreamHandler(output)
                    logger = logging.Logger("synthetic-backup-privacy")
                    logger.addHandler(handler)
                    try:
                        logger.error("synthetic backup failure", exc_info=(type(error), error, error.__traceback__))
                    finally:
                        logger.removeHandler(handler)
                        handler.close()
                    self.assertNotIn(marker, output.getvalue())
                    self.assertIn(reason.value, output.getvalue())
                else:
                    self.fail("expected classified backup failure")
                self.assertEqual((ambient.args, ambient.__cause__, ambient.__context__,
                                  ambient.__traceback__, tuple(ambient.__notes__)), before)

    def test_public_apis_do_not_retain_callers_active_exception_context(self):
        operations = (
            lambda: create_backup("relative-source", self.root / "unused-create"),
            lambda: verify_backup("relative-backup"),
            lambda: restore_backup("relative-backup", self.root / "unused-restore"),
        )
        for index, operation in enumerate(operations):
            with self.subTest(api=index):
                self.assert_ambient_private(operation, BackupFailure.PATH_REJECTED)
        self.assertFalse((self.root / "unused-create").exists())
        self.assertFalse((self.root / "unused-restore").exists())

    def test_public_error_snapshots_drop_raw_graph_without_mutating_shared_errors(self):
        marker = "SYNTHETIC_BACKUP_AMBIENT_PRIVATE_4839"
        operations = (
            lambda: create_backup(self.source, self.root / "unused-create"),
            lambda: verify_backup(self.source),
            lambda: restore_backup(self.source, self.root / "unused-restore"),
        )
        for index, operation in enumerate(operations):
            for error, reason in ((OSError(13, marker, marker + " path"), BackupFailure.IO_ERROR),
                                  (sqlite3.OperationalError(marker), BackupFailure.IO_ERROR),
                                  (BackupError(BackupFailure.BUSY), BackupFailure.BUSY)):
                error.add_note(marker + " shared note")
                original_cause = ValueError(marker + " shared cause")
                error.__cause__ = original_cause
                before_args, before_notes = error.args, tuple(error.__notes__)
                with self.subTest(api=index, error=type(error).__name__), patch.object(backup, "_root", side_effect=error):
                    self.assert_ambient_private(operation, reason)
                self.assertEqual(error.args, before_args)
                self.assertEqual(tuple(error.__notes__), before_notes)
                self.assertIs(error.__cause__, original_cause)
                # Python attaches the caller on raising this raw shared error;
                # sanitizing the public snapshot must not scrub that object.
                self.assertIsInstance(error.__context__, RuntimeError)
                self.assertIn(marker, str(error.__context__))

    def rewrite_manifest(self, destination, **changes):
        path = destination / backup.MANIFEST_FILENAME
        value = json.loads(path.read_text(encoding="utf-8"))
        value.update(changes)
        path.write_text(json.dumps(value), encoding="utf-8")

    def rehash(self, destination):
        payload = (destination / backup.SNAPSHOT_FILENAME).read_bytes()
        self.rewrite_manifest(destination, database_size=len(payload),
                              database_sha256=hashlib.sha256(payload).hexdigest())

    def test_roundtrip_preserves_identity_history_pending_and_source_bytes(self):
        before = self.database.read_bytes()
        with patch.object(FakeModel, "generate", side_effect=AssertionError("no generation")):
            destination, report = self.artifact()
            self.assertEqual(verify_backup(destination), report)
            self.assertEqual((report.session_count, report.turn_count, report.unfinished_request_count), (1, 1, 1))
            self.assertEqual(report.companion_id, self.identity.companion_id)
            self.assertEqual({p.name for p in destination.iterdir()}, {"snapshot.sqlite3", "manifest.json", LOCK_FILENAME})
            restored = restore_backup(destination, self.root / "restored")
            self.assertEqual(restored.backup, report)
            with SQLiteStore(restored.target_root, data_kind="local_preview", mode="open") as store:
                self.assertEqual((store.identity, store.day0()), (self.identity, self.day0))
                self.assertEqual(store.get_session(uid(1)), self.session)
                self.assertEqual(store.unfinished_requests(uid(1)), self.pending)
                self.assertEqual(store.attempts(uid(1), uid(3))[0].state, "running")
            self.assertEqual(self.database.read_bytes(), before)
        with self.assertRaises(FrozenInstanceError):
            report.session_count = 2
        with self.assertRaises(FrozenInstanceError):
            restored.target_root = self.source

    def test_existing_destinations_even_empty_are_never_overwritten(self):
        destination, _ = self.artifact()
        original = {p.name: p.read_bytes() for p in destination.iterdir()}
        empty = self.root / "empty"
        empty.mkdir()
        for target in (destination, empty, self.source):
            self.rejected(BackupFailure.TARGET_EXISTS, create_backup, self.source, target)
            self.rejected(BackupFailure.TARGET_EXISTS, restore_backup, destination, target)
        self.assertEqual({p.name: p.read_bytes() for p in destination.iterdir()}, original)
        self.assertEqual(tuple(empty.iterdir()), ())

    def test_paths_reject_git_relative_nested_and_link_spelling(self):
        destination, _ = self.artifact()
        for target in ("relative-new-root", self.source / "nested"):
            self.rejected(BackupFailure.PATH_REJECTED, create_backup, self.source, target)
        self.rejected(BackupFailure.PATH_REJECTED, restore_backup, destination, destination / "nested")
        git = self.root / "synthetic-git"
        git.mkdir()
        (git / ".git").mkdir()
        self.rejected(BackupFailure.PATH_REJECTED, create_backup, self.source, git / "backup")
        # Check lexical spelling without requiring Windows symlink privileges.
        missing_link = self.root / "synthetic-dangling-link"
        original = backup._is_link
        with patch.object(backup, "_is_link", side_effect=lambda path: path == missing_link or original(path)):
            self.rejected(BackupFailure.PATH_REJECTED, restore_backup, destination, missing_link)
        self.assertFalse(missing_link.exists())

    def test_missing_synthetic_and_corrupt_sources_create_no_destination(self):
        synthetic = self.root / "legacy-synthetic"
        with SQLiteStore(synthetic):
            pass
        corrupt = self.root / "corrupt-preview"
        corrupt.mkdir()
        (corrupt / backup.ACTIVE_FILENAME).write_bytes(b"synthetic non-database bytes")
        for index, source in enumerate((self.root / "missing", synthetic, corrupt)):
            destination = self.root / ("bad-source-" + str(index))
            self.rejected(BackupFailure.SOURCE_INVALID, create_backup, source, destination)
            self.assertFalse(destination.exists())

    def test_invalid_timeout_is_rejected_before_creation(self):
        for timeout in (None, True, "30", 0, -1, float("inf"), float("nan"), 10 ** 1000):
            with self.subTest(timeout=type(timeout).__name__):
                self.rejected(BackupFailure.INVALID_ARGUMENT, create_backup, self.source,
                              self.root / "bad-timeout", timeout_seconds=timeout)
        self.assertFalse((self.root / "bad-timeout").exists())

    def test_manifest_is_strict_and_refuses_duplicate_or_extra_fields(self):
        destination, _ = self.artifact()
        path = destination / backup.MANIFEST_FILENAME
        original = path.read_bytes()
        changes = ({"format_version": True}, {"session_count": True}, {"database_size": -1},
                   {"database_file": "../other.sqlite3"}, {"data_kind": "synthetic_development"},
                   {"plaintext": 1}, {"created_utc": "2026-11-05"}, {"unexpected": 1},
                   {"companion_id": "not-a-uuid"}, {"database_sha256": "A" * 64})
        for change in changes:
            with self.subTest(change=change):
                path.write_bytes(original)
                self.rewrite_manifest(destination, **change)
                self.rejected(BackupFailure.INVALID_BACKUP, verify_backup, destination)
        for payload in (original[:-2] + b',"format_version":1}\n', b" " * 65537,
                        b'{"format_version":NaN}', b"[]", b"\xff"):
            path.write_bytes(payload)
            self.rejected(BackupFailure.INVALID_BACKUP, verify_backup, destination)
        path.write_bytes(original)
        verify_backup(destination)

    def test_extra_file_sidecar_or_nonregular_fixed_file_rejected(self):
        destination, _ = self.artifact()
        for name in ("extra.txt", "snapshot.sqlite3-wal", "snapshot.sqlite3-journal"):
            extra = destination / name
            extra.write_bytes(b"synthetic")
            self.rejected(BackupFailure.INVALID_BACKUP, verify_backup, destination)
            extra.unlink()
        manifest = destination / backup.MANIFEST_FILENAME
        original = manifest.read_bytes()
        manifest.unlink()
        manifest.mkdir()
        self.rejected(BackupFailure.INVALID_BACKUP, verify_backup, destination)
        manifest.rmdir()
        manifest.write_bytes(original)
        verify_backup(destination)

    def test_damage_and_rehashed_unsupported_schema_still_fail_closed(self):
        for index, alteration in enumerate(("bytes", "application", "revision", "day0")):
            with self.subTest(alteration=alteration):
                destination, _ = self.artifact("damaged-" + str(index))
                snapshot = destination / backup.SNAPSHOT_FILENAME
                if alteration == "bytes":
                    snapshot.write_bytes(b"synthetic not sqlite")
                else:
                    with closing(sqlite3.connect(snapshot)) as connection, connection:
                        if alteration == "application":
                            connection.execute("PRAGMA application_id=0")
                        elif alteration == "revision":
                            connection.execute("UPDATE sessions SET revision=2")
                        else:
                            connection.execute("UPDATE companion SET day0_sha256=?", ("0" * 64,))
                self.rehash(destination)  # Hashes are not ledger authenticity.
                self.rejected(BackupFailure.INVALID_BACKUP, verify_backup, destination)
                target = self.root / ("invalid-restore-" + str(index))
                self.rejected(BackupFailure.INVALID_BACKUP, restore_backup, destination, target)
                self.assertFalse(target.exists())

    def test_invalid_persisted_uuid_rejected_in_source_and_rehashed_snapshot(self):
        with SQLiteStore(self.source, data_kind="local_preview", mode="open") as store:
            store.create_session(uid(90))
        destination, _ = self.artifact()
        for database in (self.database, destination / backup.SNAPSHOT_FILENAME):
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("UPDATE sessions SET session_id='not-a-uuid' WHERE session_id=?", (str(uid(90)),))
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.rejected(BackupFailure.SOURCE_INVALID, create_backup, self.source, self.root / "bad-uuid-source")
        self.rehash(destination)
        self.rejected(BackupFailure.INVALID_BACKUP, verify_backup, destination)
        self.rejected(BackupFailure.INVALID_BACKUP, restore_backup, destination, self.root / "bad-uuid-target")
        with self.assertRaises(DataIntegrityError):
            SQLiteStore(self.source, data_kind="local_preview", mode="open")
        self.assertFalse((self.root / "bad-uuid-target").exists())

    def test_linked_uuid_values_require_canonical_spelling_across_ledger(self):
        # Replace every reference together so SQL FK checks still pass. The
        # domain validator must reject IDs even when their relationships agree.
        fields = {"companion": ("companion_id", "day0_event_id"), "sessions": ("session_id",),
                  "events": ("event_id", "session_id", "root_event_id", "parent_event_id"),
                  "requests": ("session_id", "input_id", "user_event_id"),
                  "attempts": ("attempt_id", "session_id", "input_id"),
                  "turns": ("session_id", "input_id", "assistant_event_id", "attempt_id")}
        with closing(sqlite3.connect(self.database)) as connection:
            values = {row[0] for table, columns in fields.items() for column in columns
                      for row in connection.execute("SELECT " + column + " FROM " + table) if row[0] is not None}
        for index, old in enumerate(sorted(values)):
            destination, _ = self.artifact("uuid-" + str(index))
            with closing(sqlite3.connect(destination / backup.SNAPSHOT_FILENAME)) as connection, connection:
                for table, columns in fields.items():
                    assignments = ",".join(column + "=CASE WHEN " + column + "=? THEN ? ELSE " + column + " END"
                                           for column in columns)
                    connection.execute("UPDATE " + table + " SET " + assignments,
                                       (old, "{" + old + "}") * len(columns))
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.rehash(destination)
            self.rejected(BackupFailure.INVALID_BACKUP, verify_backup, destination)

    def test_wal_source_is_copied_as_independent_delete_mode_database(self):
        with SQLiteStore(self.source, data_kind="local_preview", mode="open") as store:
            self.assertEqual(store._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            store.create_session(uid(20))
            source_bytes = self.database.read_bytes()
            wal = Path(str(self.database) + "-wal")
            wal_bytes = wal.read_bytes()
            destination, report = self.artifact()
            self.assertEqual(report.session_count, 2)
            self.assertEqual(self.database.read_bytes(), source_bytes)
            self.assertEqual(wal.read_bytes(), wal_bytes)
            with closing(sqlite3.connect(destination / backup.SNAPSHOT_FILENAME)) as copied:
                self.assertEqual(copied.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            store.create_session(uid(21))
            self.assertEqual(verify_backup(destination).session_count, 2)

    def test_native_backup_deadline_leaves_unpublished_destination(self):
        destination = self.root / "expired"
        with patch.object(backup._Deadline, "progress", side_effect=BackupError(BackupFailure.TIMEOUT)):
            self.rejected(BackupFailure.TIMEOUT, create_backup, self.source, destination)
        self.assertTrue(destination.is_dir())
        self.assertFalse((destination / backup.MANIFEST_FILENAME).exists())
        self.rejected(BackupFailure.INVALID_BACKUP, verify_backup, destination)

    def test_manifest_publication_failure_is_unknown_and_never_reused(self):
        destination = self.root / "published"
        original = backup._write_manifest
        def lost_return(*args):
            original(*args)
            raise OSError("synthetic sensitive failure detail")
        with patch.object(backup, "_write_manifest", side_effect=lost_return):
            error = self.rejected(BackupFailure.OUTCOME_UNKNOWN, create_backup, self.source, destination)
        self.assertIsNone(error.__context__)
        self.assertEqual(verify_backup(destination).companion_id, self.identity.companion_id)
        self.rejected(BackupFailure.TARGET_EXISTS, create_backup, self.source, destination)

    def test_restore_revalidates_copied_bytes_against_original_manifest(self):
        destination, _ = self.artifact()
        target = self.root / "changed-during-copy"
        original = backup._copy_snapshot
        def changed_source(source, staging, deadline):
            payload = bytearray(source.read_bytes())
            payload[100] ^= 1
            source.write_bytes(payload)
            original(source, staging, deadline)
        with patch.object(backup, "_copy_snapshot", side_effect=changed_source):
            self.rejected(BackupFailure.INVALID_BACKUP, restore_backup, destination, target)
        self.assertTrue((target / backup.STAGING_FILENAME).exists())
        self.assertFalse((target / backup.ACTIVE_FILENAME).exists())
        self.rejected(BackupFailure.TARGET_EXISTS, restore_backup, destination, target)

    def test_prepublication_interrupt_preserves_exception_and_private_residue(self):
        destination, _ = self.artifact()
        for exception in (KeyboardInterrupt("synthetic interruption"), SystemExit(71)):
            target = self.root / type(exception).__name__
            with patch.object(backup, "_publish_restore", side_effect=exception):
                with self.assertRaises(type(exception)) as caught:
                    restore_backup(destination, target)
            self.assertIs(caught.exception, exception)
            self.assertTrue((target / backup.STAGING_FILENAME).exists())
            self.assertFalse((target / backup.ACTIVE_FILENAME).exists())
        self.assertEqual(verify_backup(destination).turn_count, 1)

    def test_publish_return_loss_keeps_complete_active_database_unknown(self):
        destination, _ = self.artifact()
        target = self.root / "publish-return-loss"
        original = backup._publish_restore
        def lost_return(*args):
            original(*args)
            raise OSError("synthetic failure after publication")
        with patch.object(backup, "_publish_restore", side_effect=lost_return):
            self.rejected(BackupFailure.OUTCOME_UNKNOWN, restore_backup, destination, target)
        with SQLiteStore(target, data_kind="local_preview", mode="open") as store:
            self.assertEqual(store.get_session(uid(1)), self.session)
        self.rejected(BackupFailure.TARGET_EXISTS, restore_backup, destination, target)

    def test_unsupported_hardlink_has_no_overwrite_fallback(self):
        destination, _ = self.artifact()
        target = self.root / "unsupported-link"
        with patch.object(backup.os, "link", side_effect=OSError("synthetic unsupported filesystem")):
            self.rejected(BackupFailure.IO_ERROR, restore_backup, destination, target)
        self.assertFalse((target / backup.ACTIVE_FILENAME).exists())
        self.assertTrue((target / backup.STAGING_FILENAME).is_file())

    def test_busy_source_rejected_without_destination(self):
        destination = self.root / "busy-source"
        with closing(sqlite3.connect(self.database, isolation_level=None)) as writer:
            writer.execute("BEGIN EXCLUSIVE")
            try:
                self.rejected(BackupFailure.BUSY, create_backup, self.source, destination)
            finally:
                writer.rollback()
        self.assertFalse(destination.exists())

    def test_backup_marker_must_remain_plain_empty_and_artifact_cannot_be_data_root(self):
        destination, _ = self.artifact()
        for kind in ("synthetic_development", "local_preview"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                SQLiteStore(destination, data_kind=kind, mode="create")
        verify_backup(destination)
        marker = destination / LOCK_FILENAME
        marker.write_bytes(b"synthetic invalid marker")
        self.rejected(BackupFailure.INVALID_BACKUP, verify_backup, destination)
        marker.write_bytes(b"")
        verify_backup(destination)
        marker.unlink()
        self.rejected(BackupFailure.INVALID_BACKUP, verify_backup, destination)

    def test_backup_holds_shared_lock_before_snapshot_and_rechecks_profile_winner(self):
        for kind in ("synthetic_development", "local_preview"):
            destination = self.root / ("held-" + kind)
            reached, release = threading.Event(), threading.Event()
            result = []
            original_open = Path.open
            def pause_snapshot(path, *args, **kwargs):
                if path == destination / backup.SNAPSHOT_FILENAME and args == ("xb",):
                    reached.set()
                    if not release.wait(10):
                        raise AssertionError("synthetic lock test timeout")
                return original_open(path, *args, **kwargs)
            def run():
                try:
                    result.append(create_backup(self.source, destination))
                except BaseException as error:
                    result.append(error)
            with patch.object(Path, "open", new=pause_snapshot):
                thread = threading.Thread(target=run)
                thread.start()
                try:
                    self.assertTrue(reached.wait(10))
                    with self.assertRaises(InitializationBusyError):
                        SQLiteStore(destination, data_kind=kind, mode="create")
                finally:
                    release.set()
                    thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [verify_backup(destination)])
            destination = self.root / ("profile-winner-" + kind)
            original_mkdir = Path.mkdir
            def competing_profile(path, *args, **kwargs):
                value = original_mkdir(path, *args, **kwargs)
                if path == destination and not args and not kwargs:
                    with SQLiteStore(path, data_kind=kind, mode="create"):
                        pass
                return value
            with patch.object(Path, "mkdir", new=competing_profile):
                self.rejected(BackupFailure.TARGET_EXISTS, create_backup, self.source, destination)
            self.assertFalse((destination / backup.SNAPSHOT_FILENAME).exists())
            self.assertFalse((destination / backup.MANIFEST_FILENAME).exists())

    def test_existing_store_open_remains_compatible_with_reserved_unrelated_names(self):
        # Only first creation is constrained; existing ledgers are not changed.
        (self.source / backup.MANIFEST_FILENAME).write_bytes(b"synthetic unrelated existing file")
        with SQLiteStore(self.source, data_kind="local_preview", mode="open") as store:
            self.assertEqual(store.identity, self.identity)

    def test_validation_interrupt_survives_secondary_rollback_error_and_closes_connection(self):
        original_connect = sqlite3.connect
        for interruption in (KeyboardInterrupt("synthetic validation interrupt"), SystemExit(73)):
            connections = []
            class InterruptedConnection(sqlite3.Connection):
                def execute(self, sql, *args, **kwargs):
                    if sql == "PRAGMA user_version":
                        raise interruption
                    return super().execute(sql, *args, **kwargs)
                def rollback(self):
                    super().rollback()
                    raise OSError("synthetic secondary rollback failure")
            def connect(*args, **kwargs):
                connection = original_connect(*args, factory=InterruptedConnection, **kwargs)
                connections.append(connection)
                return connection
            target = self.root / ("rollback-" + type(interruption).__name__)
            with patch.object(backup.sqlite3, "connect", side_effect=connect):
                with self.assertRaises(type(interruption)) as caught:
                    create_backup(self.source, target)
            self.assertIs(caught.exception, interruption)
            self.assertEqual(len(connections), 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                connections[0].execute("SELECT 1")
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
