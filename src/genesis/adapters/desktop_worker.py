"""One owner for a desktop session; no GUI toolkit or automatic retries.

Factories enter and exit on the worker thread. The default is memory-only;
an explicitly supplied store context is also created, used and closed there.
Queue events contain safe categories, never exception objects or tracebacks.
Closing waits for an in-flight model call; it does not cancel server inference.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from queue import Empty, Queue
import threading
from typing import Literal, Protocol
from uuid import UUID, uuid4

from genesis.core import ConversationStore, GenesisCore, Turn, TurnGenerationError, UserInput
from genesis.identity import CompanionIdentity
from genesis.models.contracts import Model, validate_model_id
from genesis.models.ollama import OllamaError
from genesis.storage._initialization import InitializationBusyError
from genesis.storage.sqlite import RecoverySnapshot, UnfinishedRequest


WorkerState = Literal["starting", "ready", "busy", "failed", "fatal", "closing", "closed"]
EventKind = Literal["ready", "succeeded", "failed", "fatal", "closed"]
StartupMode = Literal["new", "resume", "browse"]


class DesktopStore(ConversationStore, Protocol):
    """Recovery reads executed only by the thread owning the store context."""

    def session_ids(self) -> tuple[UUID, ...]: ...
    def recovery_snapshot(self, session_id: UUID) -> RecoverySnapshot: ...


class WorkerStateError(RuntimeError):
    """The requested UI action is unavailable in the current worker state."""


@dataclass(frozen=True)
class ChatEvent:
    kind: EventKind
    input_id: UUID | None = None
    turn: Turn | None = None
    error_code: str | None = None
    model_id: str | None = None
    session_id: UUID | None = None
    history: tuple[Turn, ...] = ()
    session_ids: tuple[UUID, ...] = ()
    pending_requests: tuple[UnfinishedRequest, ...] = ()
    # True identifies reconciliation after a model failure. False does not
    # prove generation or service connectivity: Core can replay committed IDs.
    recovered_commit: bool = False


class ChatWorker:
    """Nonblocking command interface for one explicitly selected session.

    ``poll_events`` acknowledges completion before another input is accepted.
    This prevents a fast model from permitting duplicate submissions while the
    UI still displays the previous operation as pending. All methods other than
    ``wait_for_events`` and an explicit nonzero ``join`` are nonblocking.

    A failed expected model call permits either ``retry`` (same immutable input)
    or ``submit`` (new input and ID). An unexpected exception closes this worker
    with a fatal category; its commit outcome must not be described as known.
    Built-in KeyboardInterrupt/SystemExit propagate as fresh exceptions of the
    same built-in type after cleanup, without original diagnostic payloads.
    Other BaseException subclasses propagate unchanged. No exception object or
    raw error message enters events.

    ``browse`` lists existing sessions without creating one; ``resume`` loads
    one exact ID without generation or changing unfinished attempts. A restored
    ``running`` attempt has an unknown execution outcome, not a live worker.
    Only an explicit ``retry(input_id)`` sends its stored, immutable input.
    """

    def __init__(
        self,
        model_factory: Callable[[], AbstractContextManager[Model]],
        *,
        store_factory: Callable[[], AbstractContextManager[DesktopStore]] | None = None,
        startup: StartupMode = "new",
        resume_session_id: UUID | None = None,
    ) -> None:
        if type(startup) is not str or startup not in ("new", "resume", "browse"):
            raise ValueError("unsupported desktop startup mode")
        if startup != "new" and store_factory is None:
            raise ValueError("browse and resume require an explicit store")
        if startup == "resume":
            if not isinstance(resume_session_id, UUID):
                raise TypeError("resume requires an explicit session UUID")
        elif resume_session_id is not None:
            raise ValueError("a session UUID is valid only for resume")
        self._model_factory = model_factory
        self._store_factory = store_factory
        self._startup = startup
        self._resume_session_id = resume_session_id
        self._lock = threading.Lock()
        self._commands: Queue[UserInput | None] = Queue()
        self._events: Queue[tuple[ChatEvent, WorkerState]] = Queue()
        self._event_available = threading.Event()
        self._state: WorkerState = "starting"
        self._retry_input: UserInput | None = None
        self._session_id: UUID | None = None
        self._known_inputs: dict[UUID, UserInput] = {}
        self._thread = threading.Thread(target=self._run, name="genesis-chat", daemon=False)
        try:
            self._thread.start()
        except BaseException:
            # start() can raise while waiting for a native thread that already
            # exists. Request cleanup even if _started/is_alive is still false;
            # a late _run will observe closing before acquiring resources.
            # Ownership never reached the caller: do not join here or close
            # worker-owned resources from this constructing thread.
            self.close()
            raise

    @property
    def state(self) -> WorkerState:
        with self._lock:
            return self._state

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def can_retry(self) -> bool:
        with self._lock:
            return self._state == "failed" and self._retry_input is not None

    def submit(self, text: str) -> UserInput:
        # Validation preserves the exact accepted text and never queues invalid
        # input. Checking the state again inside the lock handles close races.
        user_input = UserInput(uuid4(), text)
        with self._lock:
            if self._state not in ("ready", "failed") or self._session_id is None:
                raise WorkerStateError("worker does not accept a new input now")
            self._retry_input = None
            self._state = "busy"
            self._commands.put(user_input)
        return user_input

    def retry(self, input_id: UUID | None = None) -> UserInput:
        """Explicitly resend a known ID with its exact original text.

        Completed IDs are also accepted: Core replays their committed result
        without invoking the model. Callers cannot pair an old ID with new text.
        """
        if input_id is not None and not isinstance(input_id, UUID):
            raise TypeError("input_id must be a UUID")
        with self._lock:
            if self._state not in ("ready", "failed") or self._session_id is None:
                raise WorkerStateError("worker does not accept a retry now")
            if input_id is None:
                if self._state != "failed" or self._retry_input is None:
                    raise WorkerStateError("no failed input is available for retry")
                user_input = self._retry_input
            else:
                try:
                    user_input = self._known_inputs[input_id]
                except KeyError:
                    raise WorkerStateError("input does not belong to the selected session") from None
            self._state = "busy"
            self._commands.put(user_input)
        return user_input

    def close(self) -> None:
        """Request cleanup after any accepted call; never join or cancel it."""
        with self._lock:
            if self._state in ("closing", "closed", "fatal"):
                return
            self._state = "closing"
            self._commands.put(None)

    def join(self, timeout: float = 0.0) -> bool:
        """Return whether cleanup finished; GUI callers should use timeout=0."""
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def wait_for_events(self, timeout: float) -> bool:
        """Bounded synchronization for tests or non-GUI callers, not Tk callbacks."""
        return self._event_available.wait(timeout)

    def poll_events(self) -> tuple[ChatEvent, ...]:
        result: list[ChatEvent] = []
        with self._lock:
            while True:
                try:
                    event, state = self._events.get_nowait()
                except Empty:
                    break
                result.append(event)
                # A queued completion can still update the view while closing,
                # but it must not enable sending after shutdown was requested.
                if self._state != "closing" or state in ("closed", "fatal"):
                    self._state = state
            self._event_available.clear()
        return tuple(result)

    def _publish(self, event: ChatEvent, state: WorkerState) -> None:
        with self._lock:
            self._events.put((event, state))
            self._event_available.set()

    def _session_event(
        self, kind: EventKind, core: GenesisCore, store: DesktopStore | None,
        session_id: UUID | None, *, model_id: str,
        user_input: UserInput | None = None, turn: Turn | None = None,
        error_code: str | None = None,
    ) -> ChatEvent:
        """Capture immutable recovery data on the SQLite-owning thread."""
        history: tuple[Turn, ...] = ()
        pending: tuple[UnfinishedRequest, ...] = ()
        if session_id is not None:
            if store is None:
                history = core.get_session(session_id).turns
            else:
                # One read transaction keeps a concurrently committed request
                # in either history or unfinished data, never lost between two
                # independent reads. The session catalog may be newer.
                recovery = store.recovery_snapshot(session_id)
                history = recovery.session.turns
                pending = recovery.unfinished_requests
        session_ids = ((session_id,) if store is None and session_id is not None
                       else () if store is None else store.session_ids())
        recovered_commit = False
        if kind == "failed" and user_input is not None:
            # Another execution may have committed this same request while our
            # model was still running and then failed. The immutable receipt in
            # this SAME snapshot is authoritative; do not reread or generate.
            completed = next((item for item in history if item.user_input == user_input), None)
            if completed is not None:
                kind, turn, error_code = "succeeded", completed, None
                recovered_commit = True
        known = {item.user_input.input_id: item.user_input for item in history}
        known.update((item.user_input.input_id, item.user_input) for item in pending)
        if kind == "failed" and user_input is not None:
            known[user_input.input_id] = user_input
        with self._lock:
            self._session_id = session_id
            self._known_inputs = known
        return ChatEvent(kind, input_id=None if user_input is None else user_input.input_id,
                         turn=turn, error_code=error_code, model_id=model_id,
                         session_id=session_id, history=history, session_ids=session_ids,
                         pending_requests=pending, recovered_commit=recovered_commit)

    def _run(self) -> None:
        fatal_code: str | None = None
        failure_stage = "worker_startup_error"
        active_input: UserInput | None = None
        interruption: type[KeyboardInterrupt] | type[SystemExit] | None = None
        try:
            with ExitStack() as stack:
                with self._lock:
                    closing = self._state == "closing"
                if closing:
                    return
                model = stack.enter_context(self._model_factory())
                failure_stage = "storage_startup_error"
                try:
                    store = (None if self._store_factory is None
                             else stack.enter_context(self._store_factory()))
                except InitializationBusyError:
                    # Only the store-opening boundary has the pre-creation
                    # guarantee. The same exception elsewhere stays fatal
                    # under that operation's ordinary failure category.
                    failure_stage = "storage_initialization_busy"
                    raise
                failure_stage = "session_startup_error"
                identity = CompanionIdentity(uuid4()) if store is None else store.identity
                core = GenesisCore(identity, model, store=store)
                model_id = validate_model_id(model.model_id)
                if self._startup == "new":
                    session_id = core.create_session().session_id
                elif self._startup == "resume":
                    assert self._resume_session_id is not None
                    # The consistent recovery read below validates existence;
                    # no new session or automatic model request is performed.
                    session_id = self._resume_session_id
                else:
                    session_id = None
                self._publish(self._session_event("ready", core, store, session_id,
                                                  model_id=model_id), "ready")
                failure_stage = "unexpected_error"
                while True:
                    active_input = self._commands.get()
                    if active_input is None:
                        break
                    assert session_id is not None
                    try:
                        turn = core.send(session_id, active_input)
                    except TurnGenerationError as error:
                        code = (error.__cause__.reason.value
                                if isinstance(error.__cause__, OllamaError) else "model_error")
                        event = self._session_event("failed", core, store, session_id,
                                                    model_id=model_id, user_input=active_input,
                                                    error_code=code)
                        with self._lock:
                            self._retry_input = active_input if event.kind == "failed" else None
                        self._publish(event, "failed" if event.kind == "failed" else "ready")
                    else:
                        with self._lock:
                            self._retry_input = None
                        self._publish(self._session_event("succeeded", core, store, session_id,
                                                          model_id=model_id, user_input=active_input,
                                                          turn=turn), "ready")
                    active_input = None
        except Exception:
            # UI boundary: report a fatal outcome, not a retryable model failure.
            # There is deliberately no raw exception log or persisted traceback.
            fatal_code = failure_stage
        except KeyboardInterrupt:
            fatal_code = "worker_interrupted"
            interruption = KeyboardInterrupt
        except SystemExit:
            fatal_code = "worker_interrupted"
            interruption = SystemExit
        except BaseException:
            fatal_code = "worker_interrupted"
            raise
        finally:
            with self._lock:
                self._retry_input = None
                self._known_inputs = {}
            if fatal_code is not None:
                self._publish(ChatEvent("fatal", input_id=(None if active_input is None
                                                          else active_input.input_id),
                                        error_code=fatal_code), "fatal")
            self._publish(ChatEvent("closed", error_code=fatal_code),
                          "fatal" if fatal_code is not None else "closed")
        if interruption is not None:
            # Preserve interruption propagation without exposing arbitrary args,
            # notes or chained exceptions through the default thread hook.
            # Raise after cleanup and outside the original exception handler;
            # clear any ambient caller context without mutating the original.
            safe_interruption = interruption()
            try:
                raise safe_interruption from None
            except (KeyboardInterrupt, SystemExit):
                safe_interruption.__context__ = None
                raise
