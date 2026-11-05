"""Explicit plaintext local-preview backups and restoration to a new location.

Only SQLite's online backup API reads the live source. Backup manifests describe
the completed destination snapshot, never a mixture of live-source queries.
Deadlines are cooperative checks, not an OS I/O or process termination deadline.
No existing destination is reused, migrated, repaired or deleted.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from uuid import UUID

from genesis.data_paths import data_root_path
from genesis.day0 import utc_now
from genesis.storage._initialization import (
    InitializationBusyError, LOCK_FILENAME, MANIFEST_FILENAME, SNAPSHOT_FILENAME,
    STAGING_FILENAME, initialization_lock,
)
from genesis.storage.sqlite import LOCAL_PREVIEW_APPLICATION_ID, validate_ledger


ACTIVE_FILENAME = "genesis-local-preview.sqlite3"
_MANIFEST_LIMIT = 65536
_FIELDS = {"artifact_type", "format_version", "data_kind", "database_file", "database_size",
           "database_sha256", "created_utc", "companion_id", "day0_sha256", "schema_version",
           "day0_format_version", "session_count", "turn_count", "unfinished_request_count", "plaintext"}


class BackupFailure(str, Enum):
    INVALID_ARGUMENT = "invalid_argument"
    PATH_REJECTED = "path_rejected"
    TARGET_EXISTS = "target_exists"
    SOURCE_INVALID = "source_invalid"
    INVALID_BACKUP = "invalid_backup"
    BUSY = "busy"
    TIMEOUT = "timeout"
    IO_ERROR = "io_error"
    OUTCOME_UNKNOWN = "outcome_unknown"


class BackupError(RuntimeError):
    """Safe category; callers must not surface raw filesystem/SQLite errors."""

    def __init__(self, reason: BackupFailure):
        if not isinstance(reason, BackupFailure):
            raise TypeError("reason must be BackupFailure")
        self.reason = reason
        super().__init__(reason.value)


def _raise_public(reason: BackupFailure) -> None:
    # Even outside our own except block, raising can attach the caller's active
    # exception. Clear that link after raising the fresh categorical snapshot;
    # bare re-raise preserves the cleared graph without touching shared errors.
    error = BackupError(reason)
    try:
        raise error from None
    except BackupError:
        error.__context__ = None
        raise


@dataclass(frozen=True)
class BackupReport:
    backup_dir: Path
    companion_id: UUID
    day0_sha256: str
    session_count: int
    turn_count: int
    unfinished_request_count: int
    database_sha256: str
    created_utc: str


@dataclass(frozen=True)
class RestoreReport:
    target_root: Path
    backup: BackupReport


class _Deadline:
    def __init__(self, seconds):
        try:
            valid = type(seconds) in (int, float) and math.isfinite(seconds) and seconds > 0
        except OverflowError:
            valid = False
        if not valid:
            raise BackupError(BackupFailure.INVALID_ARGUMENT)
        self.expires = time.monotonic() + seconds

    def check(self):
        if time.monotonic() >= self.expires:
            raise BackupError(BackupFailure.TIMEOUT)

    def progress(self, status, remaining, total):
        # Python retries SQLITE_BUSY/LOCKED between backup steps. This callback
        # bounds those retries and restarts caused by concurrent source writes.
        self.check()

    def sqlite_progress(self):
        return int(time.monotonic() >= self.expires)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _root(value, *, new: bool = False) -> Path:
    if not isinstance(value, (str, Path)) or not value:
        raise BackupError(BackupFailure.INVALID_ARGUMENT)
    lexical = Path(value)
    # Inspect original spelling before resolve, including dangling links.
    if _is_link(lexical):
        raise BackupError(BackupFailure.PATH_REJECTED)
    if new and lexical.exists():
        raise BackupError(BackupFailure.TARGET_EXISTS)
    try:
        result = data_root_path(lexical)
    except (ValueError, TypeError, RuntimeError):
        raise BackupError(BackupFailure.PATH_REJECTED) from None
    if new and (result.exists() or _is_link(result)):
        raise BackupError(BackupFailure.TARGET_EXISTS)
    return result


def _regular(path: Path, reason: BackupFailure) -> None:
    try:
        if _is_link(path) or not stat.S_ISREG(path.lstat().st_mode):
            raise BackupError(reason)
    except FileNotFoundError:
        raise BackupError(reason) from None


def _disjoint(first: Path, second: Path) -> None:
    if first == second or first in second.parents or second in first.parents:
        raise BackupError(BackupFailure.PATH_REJECTED)


@contextmanager
def _closing(connection):
    try:
        yield connection
    except BaseException as error:
        try:
            connection.close()
        except BaseException as cleanup:
            if isinstance(error, Exception) and not isinstance(cleanup, Exception):
                raise
        raise
    else:
        connection.close()


@contextmanager
def _readonly(path: Path, deadline: _Deadline):
    deadline.check()
    with _closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                  isolation_level=None, timeout=0.2)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(deadline.sqlite_progress, 1000)
        yield connection


@contextmanager
def _read_transaction(connection):
    if connection.in_transaction:
        raise RuntimeError("backup validation requires its own read transaction")
    try:
        connection.execute("BEGIN")
        yield
    except BaseException as error:
        try:
            if connection.in_transaction:
                connection.rollback()
        except BaseException as cleanup:
            if isinstance(error, Exception) and not isinstance(cleanup, Exception):
                raise
        raise
    else:
        if connection.in_transaction:
            connection.rollback()


def _metadata(connection: sqlite3.Connection, deadline: _Deadline, reason: BackupFailure) -> dict:
    deadline.check()
    try:
        with _read_transaction(connection):
            identity = validate_ledger(connection, "local_preview", LOCAL_PREVIEW_APPLICATION_ID)
            day0 = connection.execute("SELECT day0_sha256 FROM companion").fetchone()[0]
            counts = {name: connection.execute("SELECT count(*) FROM " + table).fetchone()[0]
                      for name, table in (("session_count", "sessions"), ("turn_count", "turns"))}
            counts["unfinished_request_count"] = connection.execute(
                "SELECT count(*) FROM requests q WHERE NOT EXISTS ("
                "SELECT 1 FROM turns t WHERE t.session_id=q.session_id AND t.input_id=q.input_id)"
            ).fetchone()[0]
            deadline.check()
            return {"companion_id": str(identity.companion_id), "day0_sha256": day0, **counts}
    except sqlite3.Error as error:
        deadline.check()
        raise BackupError(BackupFailure.BUSY if _translate(error) == BackupFailure.BUSY else reason) from None
    except (ValueError, TypeError):
        deadline.check()
        raise BackupError(reason) from None


def _hash(path: Path, deadline: _Deadline) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while True:
            deadline.check()
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


def _snapshot_metadata(path: Path, deadline: _Deadline) -> dict:
    _regular(path, BackupFailure.INVALID_BACKUP)
    # A portable artifact is one closed DB, not a main file requiring sidecars.
    if any(Path(str(path) + suffix).exists() or _is_link(Path(str(path) + suffix))
           for suffix in ("-wal", "-shm", "-journal")):
        raise BackupError(BackupFailure.INVALID_BACKUP)
    try:
        with _readonly(path, deadline) as connection:
            if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise BackupError(BackupFailure.INVALID_BACKUP)
            return _metadata(connection, deadline, BackupFailure.INVALID_BACKUP)
    except sqlite3.Error:
        deadline.check()
        raise BackupError(BackupFailure.INVALID_BACKUP) from None


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate manifest key")
        result[key] = value
    return result


def _read_manifest(directory: Path, deadline: _Deadline) -> dict:
    deadline.check()
    if not directory.is_dir():
        raise BackupError(BackupFailure.INVALID_BACKUP)
    if {entry.name for entry in directory.iterdir()} != {SNAPSHOT_FILENAME, MANIFEST_FILENAME, LOCK_FILENAME}:
        raise BackupError(BackupFailure.INVALID_BACKUP)
    marker = directory / LOCK_FILENAME
    _regular(marker, BackupFailure.INVALID_BACKUP)
    if marker.stat().st_size != 0:
        raise BackupError(BackupFailure.INVALID_BACKUP)
    path = directory / MANIFEST_FILENAME
    _regular(path, BackupFailure.INVALID_BACKUP)
    _regular(directory / SNAPSHOT_FILENAME, BackupFailure.INVALID_BACKUP)
    try:
        with path.open("rb") as stream:
            payload = stream.read(_MANIFEST_LIMIT + 1)
        if len(payload) > _MANIFEST_LIMIT:
            raise ValueError("manifest too large")
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        if type(value) is not dict or set(value) != _FIELDS:
            raise ValueError("unexpected manifest fields")
        for key, expected in (("artifact_type", "genesis_local_preview_backup"),
                              ("data_kind", "local_preview"), ("database_file", SNAPSHOT_FILENAME)):
            if type(value[key]) is not str or value[key] != expected:
                raise ValueError("unsupported manifest")
        for key, expected in (("format_version", 1), ("schema_version", 1), ("day0_format_version", 2)):
            if type(value[key]) is not int or value[key] != expected:
                raise ValueError("unsupported version")
        if value["plaintext"] is not True:
            raise ValueError("plaintext classification required")
        for key in ("database_size", "session_count", "turn_count", "unfinished_request_count"):
            if type(value[key]) is not int or value[key] < (1 if key == "database_size" else 0):
                raise ValueError("invalid manifest count")
        for key in ("database_sha256", "day0_sha256"):
            if type(value[key]) is not str or not re.fullmatch(r"[0-9a-f]{64}", value[key]):
                raise ValueError("invalid digest")
        if type(value["companion_id"]) is not str or str(UUID(value["companion_id"])) != value["companion_id"]:
            raise ValueError("invalid identity")
        created = value["created_utc"]
        if type(created) is not str or not created.endswith("Z") or datetime.fromisoformat(created).utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError("invalid timestamp")
        deadline.check()
        return value
    except (ValueError, TypeError, UnicodeError, KeyError):
        raise BackupError(BackupFailure.INVALID_BACKUP) from None


def _report(directory: Path, manifest: dict) -> BackupReport:
    return BackupReport(directory, UUID(manifest["companion_id"]), manifest["day0_sha256"],
                        manifest["session_count"], manifest["turn_count"],
                        manifest["unfinished_request_count"], manifest["database_sha256"], manifest["created_utc"])


def _validate_snapshot(path: Path, manifest: dict, deadline: _Deadline) -> None:
    size, digest = _hash(path, deadline)
    if size != manifest["database_size"] or digest != manifest["database_sha256"]:
        raise BackupError(BackupFailure.INVALID_BACKUP)
    metadata = _snapshot_metadata(path, deadline)
    if any(metadata[key] != manifest[key] for key in metadata):
        raise BackupError(BackupFailure.INVALID_BACKUP)
    # Detect ordinary source changes during verification; this is not a defense
    # against a hostile filesystem writer replacing bytes at selected instants.
    if _hash(path, deadline) != (size, digest):
        raise BackupError(BackupFailure.INVALID_BACKUP)


def _write_manifest(path: Path, payload: dict, deadline: _Deadline) -> None:
    deadline.check()
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    with path.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    deadline.check()


def _copy_snapshot(source: Path, target: Path, deadline: _Deadline) -> None:
    with source.open("rb") as original, target.open("xb") as copy:
        while True:
            deadline.check()
            block = original.read(1024 * 1024)
            if not block:
                break
            copy.write(block)
        copy.flush()
        os.fsync(copy.fileno())


def _publish_restore(staging: Path, active: Path) -> None:
    # A same-filesystem hard link is atomic and refuses an existing name on
    # Windows and POSIX. No overwrite/copy fallback on unsupported filesystems.
    os.link(staging, active)
    staging.unlink()  # Only this successful operation's own staging link.


def _translate(error: Exception) -> BackupFailure:
    if isinstance(error, BackupError):
        return error.reason
    if isinstance(error, InitializationBusyError):
        return BackupFailure.BUSY
    if isinstance(error, FileExistsError):
        return BackupFailure.TARGET_EXISTS
    if isinstance(error, sqlite3.OperationalError) and getattr(error, "sqlite_errorcode", None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return BackupFailure.BUSY
    if isinstance(error, (OSError, sqlite3.Error)):
        return BackupFailure.IO_ERROR
    raise error


def create_backup(source_root, backup_dir, *, timeout_seconds=30) -> BackupReport:
    """Make a new, independently verified plaintext snapshot of local_preview.

    The source is read-only and remains usable by its owning application. A
    busy or continuously changing source may exhaust the cooperative deadline.
    Partial destinations are preserved. An error after manifest publication
    means the outcome is unknown; verify that artifact before deciding to reuse.
    """
    owned, manifest_path = False, None
    try:
        deadline = _Deadline(timeout_seconds)
        source_root, destination = _root(source_root), _root(backup_dir, new=True)
        _disjoint(source_root, destination)
        source = source_root / ACTIVE_FILENAME
        _regular(source, BackupFailure.SOURCE_INVALID)
        other = source_root / "genesis-synthetic.sqlite3"
        if other.exists() or _is_link(other):
            raise BackupError(BackupFailure.SOURCE_INVALID)
        with _readonly(source, deadline) as connection:
            _metadata(connection, deadline, BackupFailure.SOURCE_INVALID)
            destination.mkdir()  # Atomic reservation; even existing empty dirs fail.
            owned = True
            with initialization_lock(destination):
                if {entry.name for entry in destination.iterdir()} != {LOCK_FILENAME}:
                    raise BackupError(BackupFailure.TARGET_EXISTS)
                snapshot, manifest_path = destination / SNAPSHOT_FILENAME, destination / MANIFEST_FILENAME
                with snapshot.open("xb"):
                    pass
                with _closing(sqlite3.connect(snapshot.as_uri() + "?mode=rw", uri=True,
                                               isolation_level=None, timeout=0.2)) as target:
                    target.execute("PRAGMA synchronous=FULL")
                    connection.backup(target, pages=128, progress=deadline.progress, sleep=0.02)
                    deadline.check()
                    if target.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                        raise BackupError(BackupFailure.INVALID_BACKUP)
                with snapshot.open("r+b") as stream:
                    os.fsync(stream.fileno())
                metadata = _snapshot_metadata(snapshot, deadline)
                size, digest = _hash(snapshot, deadline)
                manifest = {"artifact_type": "genesis_local_preview_backup", "format_version": 1,
                            "data_kind": "local_preview", "database_file": SNAPSHOT_FILENAME,
                            "database_size": size, "database_sha256": digest, "created_utc": utc_now(),
                            "schema_version": 1, "day0_format_version": 2, "plaintext": True, **metadata}
                report = _report(destination, manifest)
                _write_manifest(manifest_path, manifest, deadline)
                if _read_manifest(destination, deadline) != manifest:
                    raise BackupError(BackupFailure.INVALID_BACKUP)
                _validate_snapshot(snapshot, manifest, deadline)
        return report
    except Exception as error:
        reason = (BackupFailure.OUTCOME_UNKNOWN if owned and manifest_path is not None and manifest_path.exists()
                  else _translate(error))
    _raise_public(reason)


def verify_backup(backup_dir, *, timeout_seconds=30) -> BackupReport:
    """Read-only artifact verification; a hash detects damage, not authenticity."""
    try:
        deadline = _Deadline(timeout_seconds)
        directory = _root(backup_dir)
        manifest = _read_manifest(directory, deadline)
        _validate_snapshot(directory / SNAPSHOT_FILENAME, manifest, deadline)
        return _report(directory, manifest)
    except Exception as error:
        reason = _translate(error)
    _raise_public(reason)


def restore_backup(backup_dir, target_root, *, timeout_seconds=30) -> RestoreReport:
    """Restore a verified snapshot to a new root, without activating a session.

    UUIDs, Day 0 and every attempt state remain unchanged. Restored 'running'
    attempts have unknown outcomes and are never executed or fenced here.
    Failures before publication leave only private staging/lock files. After
    publication, cleanup failure is an unknown outcome with the valid DB kept.
    """
    publishing, active = False, None
    try:
        deadline = _Deadline(timeout_seconds)
        directory, target = _root(backup_dir), _root(target_root, new=True)
        _disjoint(directory, target)
        manifest = _read_manifest(directory, deadline)  # One immutable in-memory manifest.
        source = directory / SNAPSHOT_FILENAME
        _validate_snapshot(source, manifest, deadline)
        report = RestoreReport(target, _report(directory, manifest))
        target.mkdir()
        with initialization_lock(target):
            # Recheck while sharing the same arbiter as both profile creators.
            if {entry.name for entry in target.iterdir()} != {LOCK_FILENAME}:
                raise BackupError(BackupFailure.TARGET_EXISTS)
            active, staging = target / ACTIVE_FILENAME, target / STAGING_FILENAME
            _copy_snapshot(source, staging, deadline)
            _validate_snapshot(staging, manifest, deadline)
            # Check both active profiles again immediately before publication.
            if {entry.name for entry in target.iterdir()} != {LOCK_FILENAME, STAGING_FILENAME}:
                raise BackupError(BackupFailure.TARGET_EXISTS)
            deadline.check()
            publishing = True
            _publish_restore(staging, active)
        return report
    except Exception as error:
        # Native link may succeed and its Python return be interrupted. Inspect
        # our exclusively reserved target instead of trusting a returned flag.
        reason = (BackupFailure.OUTCOME_UNKNOWN if publishing and active is not None and active.exists()
                  else _translate(error))
    _raise_public(reason)
