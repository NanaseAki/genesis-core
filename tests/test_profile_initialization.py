"""Real spawned initializer contention/crashes, with synthetic temporary data."""

from contextlib import contextmanager
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import genesis
from genesis.storage.sqlite import SQLiteStore


TIMEOUT = 20
LOCK_NAME = ".genesis-initialization.lock"
DATABASE_NAMES = {
    "synthetic_development": "genesis-synthetic.sqlite3",
    "local_preview": "genesis-local-preview.sqlite3",
}
EXIT_CODES = {"before_database": 81, "before_commit": 82, "after_commit": 83}


def _stored_metadata(store):
    row = store._connection.execute(
        "SELECT c.companion_id,e.content,c.day0_sha256 FROM companion c "
        "JOIN events e ON e.event_id=c.day0_event_id").fetchone()
    return {"identity": row[0], "day0": (row[1], row[2]),
            "filename": store.path.name, "data_kind": store.data_kind}


def _initializer(root, kind, mode, stop_at, channel):
    """Checkpoint only the database boundary; never barrier while holding a lock."""
    root = Path(root).resolve()
    original_open = Path.open

    def checkpoint(point, **extra):
        channel.send({"phase": "checkpoint", "point": point, **extra})
        if not channel.poll(TIMEOUT):
            raise AssertionError("synthetic initializer checkpoint timed out")
        command = channel.recv()
        if command == "crash":
            os._exit(EXIT_CODES[point])
        if command != "continue":
            raise AssertionError("unexpected synthetic initializer command")

    def observed_open(path, file_mode="r", *args, **kwargs):
        if (stop_at == "before_database" and file_mode == "xb"
                and path.parent == root and path.name == DATABASE_NAMES[kind]):
            checkpoint("before_database")
        return original_open(path, file_mode, *args, **kwargs)

    class ControlledStore(SQLiteStore):
        def _initialize(self):
            self._initializing_for_test = True
            try:
                if stop_at == "before_commit":
                    # Real SQLite rollback recovery, not just an empty cache.
                    self._connection.execute("PRAGMA cache_size = 1")
                    self._connection.execute("PRAGMA cache_spill = ON")
                super()._initialize()
            finally:
                self._initializing_for_test = False

        def _commit(self):
            initializing = getattr(self, "_initializing_for_test", False)
            if initializing and stop_at == "before_commit":
                checkpoint("before_commit")
            super()._commit()
            if initializing and stop_at == "after_commit":
                checkpoint("after_commit", metadata=_stored_metadata(self))

    try:
        channel.send({"phase": "started", "pid": os.getpid(), "module": genesis.__file__})
        with patch.object(Path, "open", observed_open):
            with ControlledStore(root, data_kind=kind, mode=mode) as store:
                metadata = _stored_metadata(store)
        channel.send({"phase": "complete", "metadata": metadata})
    except Exception as error:
        channel.send({"phase": "rejected", "error_type": type(error).__name__})
    finally:
        channel.close()


class ProfileInitializationTests(unittest.TestCase):
    def receive(self, channel, phase):
        self.assertTrue(channel.poll(TIMEOUT), "no synthetic initializer message")
        result = channel.recv()
        self.assertEqual(result["phase"], phase, result)
        return result

    @contextmanager
    def child(self, root, kind, mode="create", stop_at=None):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(target=_initializer,
                                  args=(str(root), kind, mode, stop_at, child))
        try:
            try:
                process.start()
            finally:
                child.close()
            started = self.receive(parent, "started")
            self.assertNotEqual(started["pid"], os.getpid())
            # Installed tests must spawn that same installed package, never an
            # independently injected repository source tree.
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
                self.assertFalse(process.is_alive(), "initializer process cleanup failed")
                process.close()
            parent.close()

    def finished(self, process, exit_code=0):
        process.join(TIMEOUT)
        self.assertFalse(process.is_alive(), "synthetic initializer did not exit")
        self.assertEqual(process.exitcode, exit_code)

    def run_child(self, root, kind, mode="create", *, error_type=None):
        with self.child(root, kind, mode) as (process, channel):
            result = self.receive(channel, "complete" if error_type is None else "rejected")
            self.finished(process)
            if error_type is not None:
                self.assertEqual(result["error_type"], error_type)
            return result

    def crash(self, root, kind, point):
        with self.child(root, kind, stop_at=point) as (process, channel):
            checkpoint = self.receive(channel, "checkpoint")
            self.assertEqual(checkpoint["point"], point)
            channel.send("crash")
            self.finished(process, EXIT_CODES[point])
            return checkpoint

    def database_names(self, root):
        return {name for name in DATABASE_NAMES.values() if (root / name).exists()}

    def test_different_profiles_contend_before_database_creation_in_empty_and_missing_roots(self):
        for existing_root in (False, True):
            for winner_kind in DATABASE_NAMES:
                loser_kind = next(kind for kind in DATABASE_NAMES if kind != winner_kind)
                with self.subTest(existing=existing_root, winner=winner_kind), tempfile.TemporaryDirectory(
                        prefix="genesis-profile-init-synthetic-") as folder:
                    root = Path(folder) / "data"
                    if existing_root:
                        root.mkdir()
                    with self.child(root, winner_kind, stop_at="before_database") as (winner, first):
                        checkpoint = self.receive(first, "checkpoint")
                        self.assertEqual(checkpoint["point"], "before_database")
                        self.assertEqual(self.database_names(root), set())
                        # Windows byte-range locks also reject reads across
                        # the locked byte, even when it is beyond empty EOF.
                        self.assertEqual((root / LOCK_NAME).stat().st_size, 0)
                        # The loser runs in a second process while the first
                        # owns the OS lock and has not created either database.
                        self.run_child(root, loser_kind, error_type="InitializationBusyError")
                        self.assertEqual(self.database_names(root), set())
                        first.send("continue")
                        created = self.receive(first, "complete")
                        self.finished(winner)
                    self.assertEqual(self.database_names(root), {DATABASE_NAMES[winner_kind]})
                    self.assertEqual((root / LOCK_NAME).read_bytes(), b"")
                    before = (root / DATABASE_NAMES[winner_kind]).read_bytes()
                    self.run_child(root, loser_kind, error_type="ValueError")
                    opened = self.run_child(root, winner_kind, "open")
                    self.assertEqual(opened["metadata"], created["metadata"])
                    self.assertEqual((root / DATABASE_NAMES[winner_kind]).read_bytes(), before)
                    self.assertEqual(self.database_names(root), {DATABASE_NAMES[winner_kind]})

    def test_same_profile_loser_is_busy_and_later_explicit_retry_preserves_identity(self):
        for kind in DATABASE_NAMES:
            mode = "open_or_create" if kind == "synthetic_development" else "create"
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(
                    prefix="genesis-profile-init-synthetic-") as folder:
                root = Path(folder) / "data"
                with self.child(root, kind, mode, "before_database") as (winner, first):
                    self.receive(first, "checkpoint")
                    self.run_child(root, kind, mode, error_type="InitializationBusyError")
                    self.assertEqual(self.database_names(root), set())
                    first.send("continue")
                    created = self.receive(first, "complete")
                    self.finished(winner)
                before = (root / DATABASE_NAMES[kind]).read_bytes()
                if mode == "create":
                    self.run_child(root, kind, mode, error_type="FileExistsError")
                opened = self.run_child(root, kind, "open" if mode == "create" else mode)
                self.assertEqual(opened["metadata"], created["metadata"])
                self.assertEqual((root / DATABASE_NAMES[kind]).read_bytes(), before)
                self.assertEqual(self.database_names(root), {DATABASE_NAMES[kind]})

    def test_crash_before_database_releases_os_lock_and_allows_explicit_create(self):
        for abandoned_kind in DATABASE_NAMES:
            new_kind = next(kind for kind in DATABASE_NAMES if kind != abandoned_kind)
            with self.subTest(kind=abandoned_kind), tempfile.TemporaryDirectory(
                    prefix="genesis-profile-init-synthetic-") as folder:
                root = Path(folder) / "data"
                self.crash(root, abandoned_kind, "before_database")
                self.assertEqual({path.name for path in root.iterdir()}, {LOCK_NAME})
                self.assertEqual((root / LOCK_NAME).read_bytes(), b"")
                # An empty coordination file is not a completed profile claim.
                # The process really exited without Python finally/cleanup.
                created = self.run_child(root, new_kind)
                self.assertEqual(created["metadata"]["data_kind"], new_kind)
                self.assertEqual(self.database_names(root), {DATABASE_NAMES[new_kind]})

    def test_crash_inside_initialization_never_rebuilds_or_creates_other_profile(self):
        for kind in DATABASE_NAMES:
            other_kind = next(other for other in DATABASE_NAMES if other != kind)
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(
                    prefix="genesis-profile-init-synthetic-") as folder:
                root = Path(folder) / "data"
                self.crash(root, kind, "before_commit")
                self.assertEqual(self.database_names(root), {DATABASE_NAMES[kind]})
                journal = root / (DATABASE_NAMES[kind] + "-journal")
                self.assertGreater(journal.stat().st_size, 512)
                with journal.open("rb") as stream:
                    self.assertEqual(stream.read(8), bytes.fromhex("d9d505f920a163d7"))
                self.run_child(root, kind, error_type="FileExistsError")
                self.run_child(root, other_kind, error_type="ValueError")
                # SQLite may recover a hot journal when opening; failure still
                # must not initialize a new identity over the existing file.
                self.run_child(root, kind, "open", error_type="SchemaVersionError")
                after_recovery = (root / DATABASE_NAMES[kind]).read_bytes()
                self.run_child(root, kind, "open", error_type="SchemaVersionError")
                self.assertEqual((root / DATABASE_NAMES[kind]).read_bytes(), after_recovery)
                self.assertEqual(self.database_names(root), {DATABASE_NAMES[kind]})
                self.assertTrue((root / LOCK_NAME).is_file())

    def test_crash_after_commit_open_recovers_committed_identity_without_reinitializing(self):
        for kind in DATABASE_NAMES:
            other_kind = next(other for other in DATABASE_NAMES if other != kind)
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(
                    prefix="genesis-profile-init-synthetic-") as folder:
                root = Path(folder) / "data"
                committed = self.crash(root, kind, "after_commit")
                before = (root / DATABASE_NAMES[kind]).read_bytes()
                self.run_child(root, kind, error_type="FileExistsError")
                self.run_child(root, other_kind, error_type="ValueError")
                opened = self.run_child(root, kind, "open")
                self.assertEqual(opened["metadata"], committed["metadata"])
                self.assertEqual((root / DATABASE_NAMES[kind]).read_bytes(), before)
                self.assertEqual(self.database_names(root), {DATABASE_NAMES[kind]})

    def test_legacy_synthetic_database_without_sidecar_opens_without_new_metadata(self):
        with tempfile.TemporaryDirectory(prefix="genesis-profile-init-synthetic-") as folder:
            source, legacy = Path(folder) / "source", Path(folder) / "legacy"
            created = self.run_child(source, "synthetic_development")
            original = (source / DATABASE_NAMES["synthetic_development"]).read_bytes()
            legacy.mkdir()
            path = legacy / DATABASE_NAMES["synthetic_development"]
            path.write_bytes(original)
            self.assertFalse((legacy / LOCK_NAME).exists())
            opened = self.run_child(legacy, "synthetic_development", "open_or_create")
            self.assertEqual(opened["metadata"], created["metadata"])
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual({item.name for item in legacy.iterdir()}, {path.name})

    def test_invalid_sidecar_and_existing_invalid_databases_are_not_repaired(self):
        with tempfile.TemporaryDirectory(prefix="genesis-profile-init-synthetic-") as folder:
            root = Path(folder)
            sidecar = root / LOCK_NAME
            retained = b"synthetic unrecognized coordination data"
            sidecar.write_bytes(retained)
            for kind in DATABASE_NAMES:
                self.run_child(root, kind, error_type="ValueError")
                self.assertEqual(self.database_names(root), set())
                self.assertEqual(sidecar.read_bytes(), retained)
            self.assertEqual({item.name for item in root.iterdir()}, {LOCK_NAME})
        for kind in DATABASE_NAMES:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(
                    prefix="genesis-profile-init-synthetic-") as folder:
                root = Path(folder)
                path = root / DATABASE_NAMES[kind]
                path.write_bytes(b"")
                self.run_child(root, kind, "open", error_type="SchemaVersionError")
                self.run_child(root, kind, error_type="FileExistsError")
                self.assertEqual(path.read_bytes(), b"")
                self.assertEqual({item.name for item in root.iterdir()}, {path.name})


if __name__ == "__main__":
    unittest.main()
