"""Synthetic Day 0 validation, conservative database opening and path guards."""

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from genesis.data_paths import data_root_path
from genesis.day0 import Day0ValidationError, canonical_json, make_day0, utc_now, verify_day0
from genesis.storage.sqlite import DataIntegrityError, SchemaVersionError, SQLiteStore


class Day0PathTests(unittest.TestCase):
    def test_day0_is_blank_canonical_and_verifiable(self) -> None:
        identity, created = UUID(int=1), utc_now()
        text, digest = make_day0(identity, created)
        verify_day0(text, digest, identity, created)
        value = json.loads(text)
        self.assertEqual(text, canonical_json(value))
        self.assertEqual(digest, hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertIsNone(value["initial_state"]["name"])
        self.assertIsNone(value["initial_state"]["interests"])
        self.assertIsNone(value["initial_state"]["preferences"])
        self.assertEqual(value["initial_state"]["personal_experiences"], [])
        self.assertEqual(value["initial_state"]["accepted_growth"], [])
        self.assertEqual(value["initial_state"]["permissions"], ["respond"])
        self.assertEqual(value["data_kind"], "synthetic_development")
        self.assertTrue(created.endswith("Z"))
        self.assertNotIn(str(Path.home()), text)

    def test_day0_rejects_digest_identity_version_state_and_metadata_changes(self) -> None:
        identity, created = UUID(int=1), utc_now()
        text, digest = make_day0(identity, created)
        with self.assertRaises(Day0ValidationError):
            verify_day0(text, "0" * 64, identity, created)
        with self.assertRaises(Day0ValidationError):
            verify_day0(text, digest, UUID(int=2), created)
        for field, changed in (("format_version", 2), ("schema_version", 2),
                               ("environment", {}), ("initial_state", {"interests": ["synthetic"]}),
                               ("created_utc", "2026-01-01T00:00:00"), ("data_kind", "real")):
            with self.subTest(field=field):
                value = json.loads(text)
                value[field] = changed
                altered = canonical_json(value)
                sha = hashlib.sha256(altered.encode()).hexdigest()
                with self.assertRaises(Day0ValidationError):
                    verify_day0(altered, sha, identity, value["created_utc"])

    def test_corrupt_empty_or_unrelated_database_is_not_rebuilt(self) -> None:
        for content in (b"synthetic damaged database", b""):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "genesis-synthetic.sqlite3"
                path.write_bytes(content)
                with self.assertRaises((sqlite3.DatabaseError, SchemaVersionError)):
                    SQLiteStore(root)
                self.assertEqual(path.read_bytes(), content)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "genesis-synthetic.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE unrelated(value TEXT)")
            before = path.read_bytes()
            with self.assertRaises(SchemaVersionError):
                SQLiteStore(root)
            self.assertEqual(path.read_bytes(), before)

    def test_unknown_version_or_schema_change_fails_without_mutation(self) -> None:
        for sql, error in (("PRAGMA user_version=999", SchemaVersionError),
                           ("DROP INDEX one_day0", DataIntegrityError)):
            with self.subTest(sql=sql), tempfile.TemporaryDirectory() as root:
                with SQLiteStore(root) as store:
                    path = store.path
                    store._connection.execute(sql)
                before = path.read_bytes()
                with self.assertRaises(error):
                    SQLiteStore(root)
                self.assertEqual(path.read_bytes(), before)

    def test_interrupted_initialization_is_not_silently_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with patch.object(SQLiteStore, "_commit", side_effect=KeyboardInterrupt("synthetic bootstrap failure")):
                with self.assertRaises(KeyboardInterrupt):
                    SQLiteStore(root)
            path = Path(root) / "genesis-synthetic.sqlite3"
            self.assertTrue(path.exists())
            before = path.read_bytes()
            with self.assertRaises(SchemaVersionError):
                SQLiteStore(root)
            self.assertEqual(path.read_bytes(), before)

    def test_tampered_day0_is_reported_not_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with SQLiteStore(root) as store:
                path = store.path
                store._connection.execute("UPDATE companion SET day0_sha256=?", ("0" * 64,))
            before = path.read_bytes()
            with self.assertRaises(Day0ValidationError):
                SQLiteStore(root)
            self.assertEqual(path.read_bytes(), before)

    def test_logical_ledger_corruption_and_foreign_key_damage_are_detected(self) -> None:
        from genesis.core import GenesisCore
        from genesis.models.fake import FakeModel
        for sql in ("UPDATE sessions SET revision=99", "UPDATE sessions SET companion_slot=2"):
            with self.subTest(sql=sql), tempfile.TemporaryDirectory() as root:
                with SQLiteStore(root) as store:
                    GenesisCore(store.identity, FakeModel(), store=store).create_session(UUID(int=2))
                    path = store.path
                # Simulate an out-of-band writer bypassing application constraints.
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute("PRAGMA ignore_check_constraints=ON")
                    db.execute(sql)
                before = path.read_bytes()
                with self.assertRaises(DataIntegrityError):
                    SQLiteStore(root)
                self.assertEqual(path.read_bytes(), before)

    def test_data_root_rejects_repository_and_relative_paths_before_creation(self) -> None:
        with self.assertRaises(ValueError):
            data_root_path("relative/synthetic")
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root) / "synthetic-checkout"
            env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
            env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
            subprocess.run(["git", "-c", "init.templateDir=", "init", "--quiet",
                            "--initial-branch=main", str(repo)], env=env,
                           check=True, capture_output=True, timeout=15)
            self.assertTrue((repo / ".git").is_dir())
            forbidden = repo / "synthetic-forbidden-root"
            with self.assertRaises(ValueError):
                SQLiteStore(forbidden)
            self.assertFalse(forbidden.exists())
            fake_repo = Path(root) / "synthetic-repo"
            fake_repo.mkdir()
            (fake_repo / ".git").write_text("synthetic worktree gitfile", encoding="utf-8")
            with self.assertRaises(ValueError):
                data_root_path(fake_repo / "private")

    def test_ordinary_non_git_data_root_is_allowed_and_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            temporary = Path(folder)
            root = temporary / "synthetic-ordinary-data"
            self.assertEqual(data_root_path(root), root.resolve())
            self.assertFalse(root.exists())
            with SQLiteStore(root) as store:
                self.assertEqual(store.path.parent, root.resolve())
                self.assertTrue(store.path.is_file())
            self.assertFalse((temporary / ".git").exists())
        self.assertFalse(temporary.exists())

    def test_default_windows_root_and_explicit_env_override_do_not_create_data(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with patch("genesis.data_paths.sys.platform", "win32"), patch.dict(
                os.environ, {"LOCALAPPDATA": root}, clear=True
            ):
                expected = Path(root) / "Genesis"
                self.assertEqual(data_root_path(), expected.resolve())
                self.assertFalse(expected.exists())
                override = Path(root) / "synthetic-explicit"
                with patch.dict(os.environ, {"GENESIS_DATA_ROOT": str(override)}):
                    self.assertEqual(data_root_path(), override.resolve())
                    self.assertEqual(data_root_path(Path(root) / "argument"), (Path(root) / "argument").resolve())
                self.assertFalse(override.exists())

    def test_missing_localappdata_is_explicit_error(self) -> None:
        with patch("genesis.data_paths.sys.platform", "win32"), patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                data_root_path()
