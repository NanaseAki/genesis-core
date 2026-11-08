"""Cooperative, nonblocking initialization arbitration; never delete the marker.

The empty file is a stable lock target, not an ownership record. Its presence
does not mean a process is alive. Only the OS lock and in-process mutex confer
ownership; no PID, timestamp, timeout or stale-file deletion is involved.
"""

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import threading
from typing import Iterator
from weakref import WeakValueDictionary


LOCK_FILENAME = ".genesis-initialization.lock"
SNAPSHOT_FILENAME = "snapshot.sqlite3"
MANIFEST_FILENAME = "manifest.json"
STAGING_FILENAME = ".genesis-restore-staging.sqlite3"
# A process may exit after copying a restore stage but before publication.
# Releasing its lock must not make that retained data a fresh profile root.
BACKUP_RESERVED_FILENAMES = (SNAPSHOT_FILENAME, MANIFEST_FILENAME, STAGING_FILENAME)
_registry_guard = threading.Lock()
_mutexes = WeakValueDictionary()


class InitializationBusyError(RuntimeError):
    """Another cooperating initializer holds this data root; retry explicitly."""


def _mutex_for(root: Path):
    # Keep a strong local reference while acquiring/holding the mutex. The
    # registry itself must not retain every temporary root for process lifetime.
    with _registry_guard:
        mutex = _mutexes.get(root)
        if mutex is None:
            mutex = threading.Lock()
            _mutexes[root] = mutex
        return mutex


def _acquire(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            # A lock may extend beyond EOF: the marker stays zero bytes.
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        elif os.name == "posix":
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            raise RuntimeError("initialization locking is unsupported on this platform")
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN):
            raise InitializationBusyError("data root initialization is busy; retry explicitly") from None
        raise


def _release(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    elif os.name == "posix":
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        raise RuntimeError("initialization locking is unsupported on this platform")


def _check_marker(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != 0:
        raise ValueError("initialization marker must be a regular empty file")


@contextmanager
def initialization_lock(root: Path) -> Iterator[None]:
    """Hold one root through rechecks, exclusive DB creation and validation.

    The caller creates the validated directory. Existing database opens need
    not call this helper and never acquire/create a new marker. Unsupported
    locking fails closed; this is not a hostile-filesystem or network-FS guard.
    """
    mutex = _mutex_for(root)
    if not mutex.acquire(blocking=False):
        raise InitializationBusyError("data root initialization is busy; retry explicitly")
    try:
        path = root / LOCK_FILENAME
        try:
            _check_marker(path.lstat())  # Reject symbolic links, directories and nonempty files.
        except FileNotFoundError:
            pass
        stream = path.open("a+b", buffering=0)  # Never truncate or write metadata.
        try:
            _check_marker(os.fstat(stream.fileno()))
            stream.seek(0)
            # The descriptor is already owned by this try block. If a native
            # acquire succeeds but its return is interrupted, close releases it.
            _acquire(stream.fileno())
            yield
            # On an exceptional body/acquire, closing releases the OS lock and
            # avoids an unlock error replacing the original failure/interrupt.
            _release(stream.fileno())
        except BaseException as error:
            try:
                stream.close()
            except BaseException as cleanup_error:
                # Preserve a primary interrupt. A new cleanup interrupt still
                # propagates if the primary failure was an ordinary Exception.
                if isinstance(error, Exception) and not isinstance(cleanup_error, Exception):
                    raise
            raise
        else:
            stream.close()
    finally:
        mutex.release()
