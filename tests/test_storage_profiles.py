"""Isolated preview profiles and read-only recovery using synthetic content."""

from contextlib import closing
from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import UUID

from genesis.core import (
    DuplicateInputError, GenesisCore, SessionNotFoundError, Turn, UserInput,
)
from genesis.day0 import Day0ValidationError, canonical_json, make_day0, utc_now, verify_day0
from genesis.models.contracts import Message, Role
from genesis.models.fake import FakeModel
from genesis.storage._initialization import LOCK_FILENAME
from genesis.storage.schema import APPLICATION_ID, SCHEMA_VERSION
from genesis.storage.sqlite import (
    LOCAL_PREVIEW_APPLICATION_ID, SQLiteStore, SchemaVersionError, StaleAttemptError,
)


def uid(value):
    return UUID(int=value)


def preview(root, mode):
    return SQLiteStore(root, data_kind="local_preview", mode=mode)


class StorageProfileTests(unittest.TestCase):
    def test_day0_profiles_require_exact_kind_and_version_and_preserve_default(self):
        identity, created = uid(1), utc_now()
        synthetic = make_day0(identity, created)
        self.assertEqual(synthetic, make_day0(identity, created, data_kind="synthetic_development"))
        for kind, version in (("synthetic_development", 1), ("local_preview", 2)):
            text, digest = make_day0(identity, created, data_kind=kind)
            verify_day0(text, digest, identity, created, expected_data_kind=kind)
            value = json.loads(text)
            self.assertEqual((value["data_kind"], value["format_version"], value["schema_version"]),
                             (kind, version, 1))
            self.assertEqual(value["initial_state"]["permissions"], ["respond"])
            other = "local_preview" if kind == "synthetic_development" else "synthetic_development"
            with self.assertRaises(Day0ValidationError):
                verify_day0(text, digest, identity, created, expected_data_kind=other)
            for wrong in (True, 99, 2 if version == 1 else 1):
                altered = dict(value, format_version=wrong)
                changed = canonical_json(altered)
                checksum = hashlib.sha256(changed.encode("utf-8")).hexdigest()
                with self.assertRaises(Day0ValidationError):
                    verify_day0(changed, checksum, identity, created, expected_data_kind=kind)
        with self.assertRaises(Day0ValidationError):
            verify_day0(*make_day0(identity, created, data_kind="local_preview"), identity, created)
        for invalid in (None, True, [], "LOCAL_PREVIEW", "personal"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                make_day0(identity, created, data_kind=invalid)

    def test_preview_requires_explicit_root_and_mode_before_filesystem_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "synthetic-missing"
            cases = (
                {"data_kind": "local_preview", "mode": "create"},
                {"data_root": root, "data_kind": "local_preview"},
                {"data_root": root, "data_kind": "local_preview", "mode": "OPEN"},
                {"data_root": root, "data_kind": "local_preview", "mode": None},
                {"data_root": root, "data_kind": [], "mode": "create"},
                {"data_root": "synthetic-relative", "data_kind": "local_preview", "mode": "create"},
            )
            with patch.dict(os.environ, {"GENESIS_DATA_ROOT": str(root)}), patch(
                    "genesis.storage.sqlite.sqlite3.connect", side_effect=AssertionError("must reject before connect")):
                for kwargs in cases:
                    with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                        SQLiteStore(**kwargs)
            self.assertFalse(root.exists())

    def test_create_and_open_are_explicit_and_reopening_preserves_preview_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "synthetic-preview"
            with self.assertRaises(FileNotFoundError):
                preview(root, "open")
            self.assertFalse(root.exists())
            root.mkdir()
            with self.assertRaises(FileNotFoundError):
                preview(root, "open")
            self.assertEqual(tuple(root.iterdir()), ())
            with preview(root, "create") as store:
                identity, day0, path = store.identity, store.day0(), store.path
                self.assertEqual(store.data_kind, "local_preview")
                self.assertEqual(path.name, "genesis-local-preview.sqlite3")
                self.assertEqual(store._connection.execute("PRAGMA application_id").fetchone()[0],
                                 LOCAL_PREVIEW_APPLICATION_ID)
                self.assertEqual(store._connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                self.assertNotEqual(LOCAL_PREVIEW_APPLICATION_ID, APPLICATION_ID)
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                preview(root, "create")
            with preview(root, "open") as store:
                self.assertEqual((store.identity, store.day0()), (identity, day0))
                self.assertEqual(store.session_ids(), ())
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse((root / "genesis-synthetic.sqlite3").exists())

    def test_new_preview_requires_empty_root_and_both_profiles_reject_mixed_roots(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            existing = root / "synthetic-unrelated.txt"
            existing.write_bytes(b"synthetic retained content")
            with self.assertRaises(ValueError):
                preview(root, "create")
            self.assertEqual(tuple(root.iterdir()), (existing,))
            self.assertEqual(existing.read_bytes(), b"synthetic retained content")
        for original_kind, other_kind in (("synthetic_development", "local_preview"),
                                          ("local_preview", "synthetic_development")):
            with self.subTest(original=original_kind), tempfile.TemporaryDirectory() as folder:
                root = Path(folder).resolve()
                with SQLiteStore(root, data_kind=original_kind, mode="create") as store:
                    path = store.path
                before = path.read_bytes()
                modes = ("create", "open") if other_kind == "local_preview" else ("open_or_create", "create", "open")
                with patch("genesis.storage.sqlite.sqlite3.connect", side_effect=AssertionError("must not inspect another profile")):
                    for mode in modes:
                        with self.subTest(mode=mode), self.assertRaises(ValueError):
                            SQLiteStore(root, data_kind=other_kind, mode=mode)
                self.assertEqual(set(root.iterdir()), {path, root / LOCK_FILENAME})
                self.assertEqual((root / LOCK_FILENAME).read_bytes(), b"")
                self.assertEqual(path.read_bytes(), before)

    def test_renamed_databases_fail_application_id_and_day0_profile_checks_both_ways(self):
        profiles = (("synthetic_development", "genesis-synthetic.sqlite3", APPLICATION_ID),
                    ("local_preview", "genesis-local-preview.sqlite3", LOCAL_PREVIEW_APPLICATION_ID))
        for index in (0, 1):
            source_kind, _, _ = profiles[index]
            target_kind, target_name, target_id = profiles[1 - index]
            with self.subTest(source=source_kind), tempfile.TemporaryDirectory() as folder:
                source, target = Path(folder) / "source", Path(folder) / "target"
                target.mkdir()
                with SQLiteStore(source, data_kind=source_kind, mode="create") as store:
                    source_path = store.path
                source_bytes = source_path.read_bytes()
                target_path = target / target_name
                target_path.write_bytes(source_bytes)
                with self.assertRaises(SchemaVersionError):
                    SQLiteStore(target, data_kind=target_kind, mode="open")
                self.assertEqual(target_path.read_bytes(), source_bytes)
                # A matching application ID still cannot launder the other Day 0.
                with closing(sqlite3.connect(target_path)) as connection:
                    connection.execute(f"PRAGMA application_id={target_id}")
                before = target_path.read_bytes()
                with self.assertRaises(Day0ValidationError):
                    SQLiteStore(target, data_kind=target_kind, mode="open")
                self.assertEqual(target_path.read_bytes(), before)
                self.assertEqual(source_path.read_bytes(), source_bytes)

    def test_existing_invalid_preview_files_are_preserved_without_rebuild(self):
        for data in (b"", b"synthetic invalid SQLite content"):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "genesis-local-preview.sqlite3"
                path.write_bytes(data)
                with self.assertRaises((sqlite3.DatabaseError, SchemaVersionError)):
                    preview(folder, "open")
                with self.assertRaises(FileExistsError):
                    preview(folder, "create")
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual(tuple(Path(folder).iterdir()), (path,))
        for mutation in ("PRAGMA user_version=99", "UPDATE companion SET day0_sha256='" + "0" * 64 + "'"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as folder:
                with preview(folder, "create") as store:
                    path = store.path
                    store._connection.execute(mutation)
                before = path.read_bytes()
                with self.assertRaises((SchemaVersionError, Day0ValidationError)):
                    preview(folder, "open")
                self.assertEqual(path.read_bytes(), before)

    def test_interrupted_preview_initialization_is_not_automatically_recreated(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = KeyboardInterrupt("synthetic initialization boundary")
            with patch.object(SQLiteStore, "_commit", side_effect=marker):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    preview(folder, "create")
            self.assertIs(caught.exception, marker)
            path = Path(folder) / "genesis-local-preview.sqlite3"
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                preview(folder, "create")
            with self.assertRaises(SchemaVersionError):
                preview(folder, "open")
            self.assertEqual(path.read_bytes(), before)

    def test_unfinished_discovery_is_ordered_frozen_read_only_and_excludes_committed(self):
        with tempfile.TemporaryDirectory() as folder, preview(folder, "create") as store:
            sid = uid(2)
            store.create_session(sid)
            self.assertEqual(store.unfinished_requests(sid), ())
            originals = tuple(UserInput(uid(100 - i), "  synthetic request\n" + str(i) + "  ") for i in range(5))
            running = store.begin_attempt(sid, originals[0])
            failed = store.begin_attempt(sid, originals[1])
            store.fail_attempt(failed.attempt_id, "failed", "model_error")
            interrupted = store.begin_attempt(sid, originals[2])
            store.fail_attempt(interrupted.attempt_id, "interrupted", "generation_interrupted")
            store.begin_attempt(sid, originals[3])
            latest = store.begin_attempt(sid, originals[3])
            store.fail_attempt(latest.attempt_id, "failed", "unexpected_model_error")
            stale = store.begin_attempt(sid, originals[4])
            completed_input = UserInput(uid(3), "synthetic committed input")
            core = GenesisCore(store.identity, FakeModel(["synthetic committed reply"]), store=store)
            core.send(sid, completed_input)
            with self.assertRaises(StaleAttemptError):
                store.commit_turn(stale, Turn(stale.user_input, Message(Role.ASSISTANT, "synthetic stale reply"), "synthetic-v1"))
            before = store.path.read_bytes()
            total_changes = store._connection.total_changes
            statements = []
            store._connection.set_trace_callback(statements.append)
            requests = store.unfinished_requests(sid)
            store._connection.set_trace_callback(None)
            self.assertEqual(tuple(item.user_input for item in requests), originals)
            self.assertEqual(tuple(tuple(a.state for a in item.attempts) for item in requests),
                             (("running",), ("failed",), ("interrupted",), ("unknown", "failed"), ("stale",)))
            self.assertEqual(requests[0].attempts[0].attempt_id, running.attempt_id)
            self.assertEqual(sum(line == "BEGIN" for line in statements), 1)
            self.assertEqual(sum(line == "COMMIT" for line in statements), 1)
            self.assertFalse(any(line.lstrip().split()[0] not in {"BEGIN", "SELECT", "COMMIT"} for line in statements))
            self.assertEqual(store._connection.total_changes, total_changes)
            self.assertEqual(store.path.read_bytes(), before)
            with self.assertRaises(FrozenInstanceError):
                requests[0].user_input = completed_input
            with self.assertRaises(FrozenInstanceError):
                requests[0].attempts[0].state = "failed"
            with self.assertRaises(SessionNotFoundError):
                store.unfinished_requests(uid(999))
            with self.assertRaises(TypeError):
                store.unfinished_requests(str(sid))

    def test_reopen_discovers_original_input_without_execution_until_explicit_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            sid, original = uid(2), UserInput(uid(3), "  synthetic resumed input\n原文  ")
            with preview(folder, "create") as store:
                identity, day0 = store.identity, store.day0()
                store.create_session(sid)
                first = store.begin_attempt(sid, original)
            with preview(folder, "open") as store:
                model = FakeModel(["synthetic recovered reply"])
                core = GenesisCore(store.identity, model, store=store)
                self.assertEqual((store.identity, store.day0()), (identity, day0))
                self.assertEqual(store.session_ids(), (sid,))
                self.assertEqual(core.get_session(sid).turns, ())
                unfinished = store.unfinished_requests(sid)
                self.assertEqual(unfinished[0].user_input, original)
                self.assertEqual(unfinished[0].attempts[0].state, "running")
                self.assertEqual(model.requests, ())
                with self.assertRaises(DuplicateInputError):
                    core.send(sid, UserInput(original.input_id, "synthetic changed content"))
                completed = core.send(sid, unfinished[0].user_input)
                self.assertEqual(len(model.requests), 1)
                self.assertEqual(store.unfinished_requests(sid), ())
                self.assertEqual(tuple(a.state for a in store.attempts(sid, original.input_id)), ("unknown", "committed"))
                self.assertEqual(store.attempts(sid, original.input_id)[0].attempt_id, first.attempt_id)
            with preview(folder, "open") as store:
                model = FakeModel([])
                core = GenesisCore(store.identity, model, store=store)
                self.assertEqual(core.send(sid, original), completed)
                self.assertEqual(model.requests, ())
                self.assertEqual(len(core.get_session(sid).turns), 1)

    def test_recovery_snapshot_keeps_history_and_unfinished_consistent_during_commit(self):
        with tempfile.TemporaryDirectory() as folder, preview(folder, "create") as reader:
            sid, original = uid(2), UserInput(uid(3), "synthetic concurrent recovery input")
            reader.create_session(sid)
            attempt = reader.begin_attempt(sid, original)
            turn = Turn(original, Message(Role.ASSISTANT, "synthetic concurrent reply"), "synthetic-v1")
            ready, start, committing, done = (threading.Event() for _ in range(4))
            errors = []

            def writer():
                try:
                    with preview(folder, "open") as store:
                        commit = store._commit

                        def observed_commit():
                            committing.set()
                            commit()

                        store._commit = observed_commit
                        ready.set()
                        if not start.wait(5):
                            raise AssertionError("recovery reader did not begin")
                        store.commit_turn(attempt, turn)
                except BaseException as error:
                    errors.append(error)
                    ready.set()
                finally:
                    done.set()

            thread = threading.Thread(target=writer, name="synthetic-preview-writer")
            thread.start()
            try:
                self.assertTrue(ready.wait(5))
                self.assertEqual(errors, [])
                snapshot = reader._snapshot

                def read_history_then_start_commit(db, session_id):
                    result = snapshot(db, session_id)
                    start.set()
                    self.assertTrue(committing.wait(5), "writer did not reach its commit boundary")
                    self.assertFalse(done.is_set(), "writer committed while the read snapshot was active")
                    return result

                statements = []
                reader._connection.set_trace_callback(statements.append)
                with patch.object(reader, "_snapshot", side_effect=read_history_then_start_commit):
                    recovered = reader.recovery_snapshot(sid)
                reader._connection.set_trace_callback(None)
                self.assertEqual(recovered.session.turns, ())
                self.assertEqual(tuple(item.user_input for item in recovered.unfinished_requests), (original,))
                self.assertEqual(recovered.unfinished_requests[0].attempts[0].state, "running")
                self.assertEqual(sum(line == "BEGIN" for line in statements), 1)
                self.assertEqual(sum(line == "COMMIT" for line in statements), 1)
                self.assertTrue(done.wait(5))
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                refreshed = reader.recovery_snapshot(sid)
                self.assertEqual(refreshed.session.turns, (turn,))
                self.assertEqual(refreshed.unfinished_requests, ())
                with self.assertRaises(FrozenInstanceError):
                    recovered.session = refreshed.session
            finally:
                reader._connection.set_trace_callback(None)
                start.set()
                thread.join(6)
                self.assertFalse(thread.is_alive(), "concurrent writer cleanup failed")


if __name__ == "__main__":
    unittest.main()
