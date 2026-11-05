"""One explicit data operation, owned by a separate non-daemon worker.

No model, chat session, or existing SQLite connection crosses this boundary.
Storage functions acquire and release their own resources on this thread.
Closing requests cleanup, not cancellation of an operation already started.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
import threading
from typing import Literal
from uuid import UUID, uuid4

from genesis.storage.backup import (
    BackupError, BackupFailure, BackupReport, RestoreReport,
    create_backup, restore_backup, verify_backup,
)


DataOperation = Literal["backup", "verify", "restore"]
DataTaskState = Literal["starting", "running", "closing", "closed"]
DataEventKind = Literal["succeeded", "failed", "fatal", "closed"]
DataTaskReport = BackupReport | RestoreReport
_EXPECTED_FAILURES = frozenset({
    "invalid_argument", "path_rejected", "target_exists", "source_invalid",
    "invalid_backup", "busy", "timeout", "io_error", "outcome_unknown",
})


def _absolute_path(value: str) -> None:
    # Syntax only. Filesystem inspection and SQLite acquisition belong to the
    # operation thread, including checks for aliases and existing destinations.
    if type(value) is not str or not value or "\0" in value:
        raise ValueError("an explicit absolute path is required")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("path must be valid UTF-8 text") from None
    if not Path(value).is_absolute():
        raise ValueError("an explicit absolute path is required")


@dataclass(frozen=True)
class DataTaskRequest:
    operation: DataOperation
    source_root: str
    destination_root: str | None = None
    plaintext_acknowledged: bool = False
    operation_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if type(self.operation) is not str or self.operation not in {"backup", "verify", "restore"}:
            raise ValueError("unsupported data operation")
        if type(self.plaintext_acknowledged) is not bool:
            raise TypeError("plaintext acknowledgment must be a boolean")
        if not isinstance(self.operation_id, UUID):
            raise TypeError("operation_id must be a UUID")
        _absolute_path(self.source_root)
        if self.operation == "verify":
            if self.destination_root is not None:
                raise ValueError("verification does not select a destination")
        else:
            if not self.plaintext_acknowledged:
                raise ValueError("writing a data copy requires explicit plaintext acknowledgment")
            _absolute_path(self.destination_root)


@dataclass(frozen=True)
class DataTaskEvent:
    kind: DataEventKind
    operation_id: UUID
    operation: DataOperation
    report: DataTaskReport | None = None
    error_code: str | None = None


def _perform(request: DataTaskRequest) -> DataTaskReport:
    if request.operation == "backup":
        return create_backup(request.source_root, request.destination_root)
    if request.operation == "verify":
        return verify_backup(request.source_root)
    return restore_backup(request.source_root, request.destination_root)


class DataTaskWorker:
    """Start one immutable request; there is no queue or automatic retry.

    ``poll_events`` returns safe immutable results. Keep the owner until a
    ``closed`` event has been consumed AND ``is_alive`` is false: the final
    event can be published just before the native thread returns. A close
    before work begins skips the operation; after work begins it runs to its
    result and releases its own resources. Neither close nor join cancels it.

    Built-in KeyboardInterrupt/SystemExit are reported and propagated as new,
    payload-free exceptions of the same type after cleanup. Other BaseException
    subclasses propagate unchanged. Constructor failures preserve the original
    exception and request close even when the native thread is not yet visible.
    """

    def __init__(self, request: DataTaskRequest, *,
                 operation_runner: Callable[[DataTaskRequest], DataTaskReport] | None = None) -> None:
        if type(request) is not DataTaskRequest:
            raise TypeError("request must be a DataTaskRequest")
        if operation_runner is not None and not callable(operation_runner):
            raise TypeError("operation_runner must be callable")
        self._request = request
        self._operation_runner = _perform if operation_runner is None else operation_runner
        self._lock = threading.Lock()
        self._events: Queue[DataTaskEvent] = Queue()
        self._event_available = threading.Event()
        self._state: DataTaskState = "starting"
        self._thread = threading.Thread(target=self._run, name="genesis-data-task", daemon=False)
        try:
            self._thread.start()
        except BaseException:
            # start() may have created a native thread before raising while
            # waiting for its started signal. A late _run must see closing.
            self.close()
            raise

    @property
    def request(self) -> DataTaskRequest:
        return self._request

    @property
    def state(self) -> DataTaskState:
        with self._lock:
            return self._state

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def close(self) -> None:
        """Do not wait, interrupt the runner, or discard its eventual result."""
        with self._lock:
            if self._state != "closed":
                self._state = "closing"

    def join(self, timeout: float = 0.0) -> bool:
        """Nonzero waits are for tests/non-GUI callers, never Tk callbacks."""
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def wait_for_events(self, timeout: float) -> bool:
        """Bounded synchronization for tests, not the Tk thread."""
        return self._event_available.wait(timeout)

    def poll_events(self) -> tuple[DataTaskEvent, ...]:
        with self._lock:
            result: list[DataTaskEvent] = []
            while True:
                try:
                    result.append(self._events.get_nowait())
                except Empty:
                    break
            self._event_available.clear()
            return tuple(result)

    def _publish(self, kind: DataEventKind, *, report: DataTaskReport | None = None,
                 error_code: str | None = None) -> None:
        event = DataTaskEvent(kind, self.request.operation_id, self.request.operation, report, error_code)
        with self._lock:
            self._events.put(event)
            if kind == "closed":
                self._state = "closed"
            self._event_available.set()

    def _run(self) -> None:
        error_code: str | None = None
        interruption: type[KeyboardInterrupt] | type[SystemExit] | None = None
        try:
            with self._lock:
                if self._state == "closing":
                    return
                self._state = "running"
            report = self._operation_runner(self.request)
            expected_type = RestoreReport if self.request.operation == "restore" else BackupReport
            if type(report) is not expected_type:
                raise TypeError("data operation returned an invalid report")
            self._publish("succeeded", report=report)
        except BackupError as error:
            # Never forward arbitrary reasons, diagnostic strings, or chains.
            reason = getattr(error, "reason", None) if type(error) is BackupError else None
            if type(reason) is BackupFailure and reason.value in _EXPECTED_FAILURES:
                error_code = "data_" + reason.value
                self._publish("failed", error_code=error_code)
            else:
                error_code = "data_internal_error"
                self._publish("fatal", error_code=error_code)
        except Exception:
            error_code = "data_internal_error"
            self._publish("fatal", error_code=error_code)
        except KeyboardInterrupt:
            error_code = "data_interrupted"
            interruption = KeyboardInterrupt
            self._publish("fatal", error_code=error_code)
        except SystemExit:
            error_code = "data_interrupted"
            interruption = SystemExit
            self._publish("fatal", error_code=error_code)
        except BaseException:
            error_code = "data_interrupted"
            self._publish("fatal", error_code=error_code)
            raise
        finally:
            self._publish("closed", error_code=error_code)
        if interruption is not None:
            safe_interruption = interruption()
            try:
                raise safe_interruption from None
            except (KeyboardInterrupt, SystemExit):
                safe_interruption.__context__ = None
                raise
