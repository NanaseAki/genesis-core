"""Conversation orchestration; the original in-memory path remains available."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol
from uuid import UUID, uuid4

from genesis.identity import CompanionIdentity
from genesis.models.contracts import (
    Message, Model, ModelError, ModelReply, ModelRequest, Role, validate_model_id,
)
from genesis.policy import Action, DefaultPolicy, Policy
from genesis.text import TextContractError, validate_text


class SessionNotFoundError(LookupError):
    pass


class SessionExistsError(ValueError):
    pass


class DuplicateInputError(ValueError):
    """An input ID was reused with different content in the same session."""


class PolicyDeniedError(PermissionError):
    pass


class TurnGenerationError(RuntimeError):
    """Expected model failure; no successful turn was committed."""


@dataclass(frozen=True)
class UserInput:
    input_id: UUID
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.input_id, UUID):
            raise TypeError("input_id must be a UUID")
        validate_text(self.content, field="input content")


@dataclass(frozen=True)
class Turn:
    user_input: UserInput
    assistant: Message
    model_id: str


@dataclass(frozen=True)
class SessionSnapshot:
    session_id: UUID
    companion_id: UUID
    turns: tuple[Turn, ...]

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(message for turn in self.turns for message in (
            Message(Role.USER, turn.user_input.content), turn.assistant
        ))


@dataclass(frozen=True)
class _Session:
    turns: tuple[Turn, ...] = ()
    completed_inputs: Mapping[UUID, Turn] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True)
class PendingAttempt:
    attempt_id: UUID
    snapshot: SessionSnapshot
    user_input: UserInput


class ConversationStore(Protocol):
    """Trusted host service; never passed to a model.

    begin_attempt binds input content and captures a consistent snapshot.
    commit_turn atomically publishes history AND the idempotency receipt, or
    raises. Generation takes place between these short transactions. A lost
    return after commit is recovered by lookup, not by repeating publication.
    """

    @property
    def identity(self) -> CompanionIdentity: ...
    def create_session(self, session_id: UUID) -> SessionSnapshot: ...
    def get_session(self, session_id: UUID) -> SessionSnapshot: ...
    def lookup(self, session_id: UUID, user_input: UserInput) -> Turn | None: ...
    def begin_attempt(self, session_id: UUID, user_input: UserInput) -> PendingAttempt | Turn: ...
    def commit_turn(self, attempt: PendingAttempt, turn: Turn) -> Turn: ...
    def fail_attempt(
        self, attempt_id: UUID, state: Literal["failed", "interrupted"], code: str
    ) -> None: ...


class GenesisCore:
    def __init__(
        self,
        identity: CompanionIdentity,
        model: Model,
        policy: Policy | None = None,
        *,
        store: ConversationStore | None = None,
    ) -> None:
        self._identity = identity
        self._model = model
        self._policy = policy if policy is not None else DefaultPolicy()
        self._sessions: dict[UUID, _Session] = {}
        if store is not None and store.identity != identity:
            raise ValueError("store belongs to another companion")
        self._store = store

    @property
    def identity(self) -> CompanionIdentity:
        return self._identity

    def create_session(self, session_id: UUID | None = None) -> SessionSnapshot:
        session_id = uuid4() if session_id is None else session_id
        if not isinstance(session_id, UUID):
            raise TypeError("session_id must be a UUID")
        if self._store is not None:
            return self._store.create_session(session_id)
        if session_id in self._sessions:
            raise SessionExistsError("session already exists")
        self._sessions[session_id] = _Session()
        return self.get_session(session_id)

    def get_session(self, session_id: UUID) -> SessionSnapshot:
        if self._store is not None:
            return self._store.get_session(session_id)
        try:
            session = self._sessions[session_id]
        except KeyError:
            raise SessionNotFoundError("session does not exist") from None
        return SessionSnapshot(
            session_id, self._identity.companion_id, tuple(session.turns)
        )

    def send(self, session_id: UUID, user_input: UserInput) -> Turn:
        """Send one input, replaying completed IDs without new generation.

        The in-memory path prepares a complete state before single publication;
        failure before that point leaves the old in-memory state untouched.
        Interruption after publication can hide the return, but retry reads the
        committed Turn. KeyboardInterrupt is never swallowed. With a store,
        accepted inputs/attempts may survive failure; successful publication and
        retry follow ConversationStore and its documented transaction rules.
        """
        if self._store is not None:
            return self._send_persisted(session_id, user_input)
        snapshot = self.get_session(session_id)
        session = self._sessions[session_id]
        previous = session.completed_inputs.get(user_input.input_id)
        if previous is not None:
            if previous.user_input != user_input:
                raise DuplicateInputError("input ID already has different content")
            return previous
        decision = self._policy.authorize(Action.RESPOND)
        if not decision.allowed:
            raise PolicyDeniedError("reply denied by policy")
        request = ModelRequest(
            companion_id=self._identity.companion_id,
            session_id=session_id,
            input_id=user_input.input_id,
            messages=snapshot.messages + (Message(Role.USER, user_input.content),),
        )
        try:
            model_id = validate_model_id(self._model.model_id)
            reply = self._model.generate(request)
            if not isinstance(reply, ModelReply):
                raise ModelError("invalid_model_reply")
            validate_text(reply.content, field="reply content")
        except (ModelError, TextContractError) as error:
            raise TurnGenerationError("model failed; no turn was committed") from error
        turn = Turn(user_input, Message(Role.ASSISTANT, reply.content), model_id)
        completed_inputs = dict(session.completed_inputs)
        completed_inputs[user_input.input_id] = turn
        new_session = _Session(
            session.turns + (turn,), MappingProxyType(completed_inputs)
        )
        self._sessions[session_id] = new_session  # The single logical commit point.
        return turn

    def _send_persisted(self, session_id: UUID, user_input: UserInput) -> Turn:
        store = self._store
        assert store is not None
        previous = store.lookup(session_id, user_input)
        if previous is not None:
            return previous
        if not self._policy.authorize(Action.RESPOND).allowed:
            raise PolicyDeniedError("reply denied by policy")
        attempt = store.begin_attempt(session_id, user_input)
        if isinstance(attempt, Turn):
            return attempt  # Another writer may have committed since lookup.
        try:
            request = ModelRequest(
                self._identity.companion_id, session_id, user_input.input_id,
                attempt.snapshot.messages + (Message(Role.USER, user_input.content),),
            )
            model_id = validate_model_id(self._model.model_id)
            reply = self._model.generate(request)
            if not isinstance(reply, ModelReply):
                raise ModelError("invalid_model_reply")
            validate_text(reply.content, field="reply content")
        except (ModelError, TextContractError) as error:
            store.fail_attempt(attempt.attempt_id, "failed", "model_error")
            raise TurnGenerationError("model failed; no turn was committed") from error
        except BaseException as error:
            state = "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed"
            store.fail_attempt(attempt.attempt_id, state, "generation_interrupted"
                               if state == "interrupted" else "unexpected_model_error")
            raise
        # Do not mark generation failed when publication/return is interrupted:
        # its durable outcome is resolved on retry. Never swallow BaseException.
        turn = Turn(user_input, Message(Role.ASSISTANT, reply.content), model_id)
        return store.commit_turn(attempt, turn)
