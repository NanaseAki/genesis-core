"""Backend-independent synchronous text contract, with no state write capability."""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from uuid import UUID

from genesis.text import TextContractError, validate_text


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class Message:
    role: Role
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, Role):
            raise TypeError("role must be a Role")
        validate_text(self.content, field="message content")


@dataclass(frozen=True)
class ModelRequest:
    companion_id: UUID
    session_id: UUID
    input_id: UUID
    messages: tuple[Message, ...]

    def __post_init__(self) -> None:
        if not all(isinstance(value, UUID) for value in (
            self.companion_id, self.session_id, self.input_id
        )):
            raise TypeError("request IDs must be UUIDs")
        if not isinstance(self.messages, tuple) or not self.messages:
            raise ValueError("messages must be a nonempty tuple")
        if not all(isinstance(message, Message) for message in self.messages):
            raise TypeError("messages must contain Message values")
        if self.messages[-1].role is not Role.USER:
            raise ValueError("the final message must be user input")


@dataclass(frozen=True)
class ModelReply:
    content: str

    def __post_init__(self) -> None:
        validate_text(self.content, field="reply content")


class ModelError(Exception):
    """Expected adapter failure. Adapters must use sanitized error messages."""


def validate_model_id(value: object) -> str:
    """Validate diagnostic metadata without letting SQLite coerce its type."""
    try:
        return validate_text(value, field="model_id")
    except TextContractError as error:
        raise ModelError("invalid_model_id") from error


class Model(Protocol):
    @property
    def model_id(self) -> str:
        """Nonblank NUL-free UTF-8 string; never the companion's identity."""
        ...

    def generate(self, request: ModelRequest) -> ModelReply:
        """Return a complete reply or raise ModelError; never fall back silently.

        Requests contain immutable text and IDs only. Streaming, cancellation
        and tool calls are future contract work. Backend timeout configuration
        belongs to the injected adapter; this protocol promises no total deadline.
        """
        ...
