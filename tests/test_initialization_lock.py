"""Synthetic lock ownership failures; no real data or native GUI is used."""

import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from genesis.storage import _initialization as locks
from genesis.storage.sqlite import SQLiteStore


class CloseFailure:
    """Close the real owned resource, then inject a synthetic return failure."""

    def __init__(self, resource, error):
        self.resource, self.error = resource, error

    def __getattr__(self, name):
        return getattr(self.resource, name)

    @property
    def row_factory(self):
        return self.resource.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self.resource.row_factory = value

    def close(self):
        self.resource.close()
        raise self.error


class InitializationLockTests(unittest.TestCase):
    def test_invalid_marker_is_preserved_and_releases_the_process_mutex(self):
        for marker_kind in ("nonempty", "directory", "symbolic_metadata"):
            with self.subTest(marker_kind=marker_kind), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                marker = root / locks.LOCK_FILENAME
                mutex = locks._mutex_for(root)
                if marker_kind == "nonempty":
                    marker.write_bytes(b"synthetic unrelated contents")
                elif marker_kind == "directory":
                    marker.mkdir()
                if marker_kind == "symbolic_metadata":
                    # Exercise the symbolic-link metadata branch without
                    # requiring Windows symlink privileges in the test runner.
                    with patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=stat.S_IFLNK, st_size=0)), patch.object(
                            Path, "open", side_effect=AssertionError("must reject before opening a link")):
                        with self.assertRaises(ValueError):
                            with locks.initialization_lock(root):
                                self.fail("invalid marker acquired")
                else:
                    with self.assertRaises(ValueError):
                        with locks.initialization_lock(root):
                            self.fail("invalid marker acquired")
                self.assertFalse(mutex.locked())
                if marker_kind == "nonempty":
                    self.assertEqual(marker.read_bytes(), b"synthetic unrelated contents")
                elif marker_kind == "directory":
                    self.assertTrue(marker.is_dir())
                    self.assertEqual(tuple(marker.iterdir()), ())
                else:
                    self.assertFalse(marker.exists())
                self.assertFalse((root / "genesis-synthetic.sqlite3").exists())
                self.assertFalse((root / "genesis-local-preview.sqlite3").exists())

    def test_native_acquire_return_failure_closes_descriptor_and_releases_mutex(self):
        for error in (RuntimeError("synthetic acquire return loss"), KeyboardInterrupt("synthetic acquire interrupt"), SystemExit(17)):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                mutex, descriptors = locks._mutex_for(root), []
                acquire = locks._acquire

                def acquire_then_fail(fd):
                    acquire(fd)
                    descriptors.append(fd)
                    raise error

                with patch.object(locks, "_acquire", side_effect=acquire_then_fail):
                    with self.assertRaises(type(error)) as caught:
                        with locks.initialization_lock(root):
                            self.fail("acquire failure must not enter the body")
                self.assertIs(caught.exception, error)
                self.assertFalse(mutex.locked())
                with self.assertRaises(OSError):
                    os.fstat(descriptors[0])
                with locks.initialization_lock(root):
                    self.assertEqual((root / locks.LOCK_FILENAME).stat().st_size, 0)
                self.assertEqual((root / locks.LOCK_FILENAME).read_bytes(), b"")

    def test_body_interrupt_keeps_original_when_marker_close_also_fails(self):
        for error in (RuntimeError("synthetic primary"), KeyboardInterrupt("synthetic primary"), SystemExit(18)):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                marker = root / locks.LOCK_FILENAME
                mutex, streams = locks._mutex_for(root), []
                real_open = Path.open

                def failed_close(path, *args, **kwargs):
                    stream = real_open(path, *args, **kwargs)
                    if path == marker:
                        streams.append(stream)
                        return CloseFailure(stream, RuntimeError("synthetic secondary close failure"))
                    return stream

                with patch.object(Path, "open", failed_close), patch.object(
                        locks, "_release", side_effect=AssertionError("exception cleanup must preserve primary")):
                    with self.assertRaises(type(error)) as caught:
                        with locks.initialization_lock(root):
                            raise error
                self.assertIs(caught.exception, error)
                self.assertTrue(streams[0].closed)
                self.assertFalse(mutex.locked())
                with locks.initialization_lock(root):
                    pass

    def test_unlock_or_marker_close_failure_closes_unreturned_sqlite_connection(self):
        for stage in ("unlock", "close"):
            for error in (RuntimeError("synthetic lock exit failure"), KeyboardInterrupt("synthetic lock exit interrupt"), SystemExit(19)):
                with self.subTest(stage=stage, error=type(error).__name__), tempfile.TemporaryDirectory() as folder:
                    root, connections, identities = Path(folder), [], []
                    marker = root / locks.LOCK_FILENAME
                    validate, real_open = SQLiteStore._validate, Path.open

                    def validated(store):
                        validate(store)
                        connections.append(store._connection)
                        identities.append(store.identity)

                    def failed_close(path, *args, **kwargs):
                        stream = real_open(path, *args, **kwargs)
                        return CloseFailure(stream, error) if path == marker else stream

                    failure = (patch.object(locks, "_release", side_effect=error) if stage == "unlock"
                               else patch.object(Path, "open", failed_close))
                    with failure, patch.object(SQLiteStore, "_validate", validated):
                        with self.assertRaises(type(error)) as caught:
                            SQLiteStore(root, data_kind="local_preview", mode="create")
                    self.assertIs(caught.exception, error)
                    self.assertEqual(len(connections), 1)
                    with self.assertRaises(sqlite3.ProgrammingError):
                        connections[0].execute("SELECT 1")
                    with SQLiteStore(root, data_kind="local_preview", mode="open") as reopened:
                        self.assertEqual(reopened.identity, identities[0])
                    with locks.initialization_lock(root):
                        pass

    def test_sqlite_primary_interrupt_survives_secondary_connection_close_failure(self):
        for error in (KeyboardInterrupt("synthetic initialization interruption"), SystemExit(20)):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as folder:
                root, connections = Path(folder), []
                connect = sqlite3.connect

                def failed_close(*args, **kwargs):
                    connection = connect(*args, **kwargs)
                    connections.append(connection)
                    return CloseFailure(connection, RuntimeError("synthetic secondary database close failure"))

                with patch("genesis.storage.sqlite.sqlite3.connect", side_effect=failed_close), patch.object(
                        SQLiteStore, "_initialize", side_effect=error):
                    with self.assertRaises(type(error)) as caught:
                        SQLiteStore(root, data_kind="local_preview", mode="create")
                self.assertIs(caught.exception, error)
                with self.assertRaises(sqlite3.ProgrammingError):
                    connections[0].execute("SELECT 1")
                self.assertTrue((root / "genesis-local-preview.sqlite3").is_file())
                with locks.initialization_lock(root):
                    pass

    def test_same_root_thread_contends_without_serializing_other_roots(self):
        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder) / "first", Path(folder) / "second"
            first.mkdir()
            second.mkdir()
            results = []

            def contender():
                try:
                    with locks.initialization_lock(first):
                        results.append("unexpected acquisition")
                except locks.InitializationBusyError:
                    results.append("busy")
                with locks.initialization_lock(second):
                    results.append("independent root")

            with locks.initialization_lock(first):
                thread = threading.Thread(target=contender)
                thread.start()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results, ["busy", "independent root"])
            with locks.initialization_lock(first):
                pass


if __name__ == "__main__":
    unittest.main()
