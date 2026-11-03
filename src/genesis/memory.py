"""Provenance/record contracts; event persistence lives in the host storage layer."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from genesis.text import validate_text


class EvidenceOrigin(str, Enum):
    USER_INPUT = "user_input"
    TOOL_RESULT = "tool_result"
    MODEL_OUTPUT = "model_output"


class MemoryKind(str, Enum):
    OBSERVATION = "observation"
    INFERENCE = "inference"


@dataclass(frozen=True)
class EvidenceReference:
    event_id: UUID
    origin: EvidenceOrigin
    root_event_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, UUID) or not isinstance(self.root_event_id, UUID):
            raise TypeError("evidence IDs must be UUIDs")
        if not isinstance(self.origin, EvidenceOrigin):
            raise TypeError("origin must be an EvidenceOrigin")


@dataclass(frozen=True, init=False)
class TrustedEvidenceRegistry:
    """Immutable attestations supplied separately by trusted host ingestion.

    Never construct this registry from a model's proposed sources. A reference
    is confirmed only if it matches the host's event and its direct root is also
    registered as a root. This verifies provenance, not truth or preference.
    M2-A can rebuild this snapshot from its host-owned event ledger. No model
    gets this authority through ModelRequest or GrowthCandidate.
    """

    _events: Mapping[UUID, EvidenceReference]

    def __init__(self, events: Iterable[EvidenceReference] = ()) -> None:
        registered: dict[UUID, EvidenceReference] = {}
        for event in events:
            if not isinstance(event, EvidenceReference):
                raise TypeError("registry events must be EvidenceReference values")
            previous = registered.get(event.event_id)
            if previous is not None and previous != event:
                raise ValueError("conflicting attestations for one event ID")
            registered[event.event_id] = event
        object.__setattr__(self, "_events", MappingProxyType(registered))

    def independent_roots(
        self, sources: tuple[EvidenceReference, ...]
    ) -> frozenset[UUID]:
        roots: set[UUID] = set()
        for source in sources:
            if self._events.get(source.event_id) != source:
                continue
            root = self._events.get(source.root_event_id)
            if root is None or root.event_id != root.root_event_id:
                continue
            if root.origin in (EvidenceOrigin.USER_INPUT, EvidenceOrigin.TOOL_RESULT):
                roots.add(root.event_id)
        return frozenset(roots)


@dataclass(frozen=True)
class MemoryRecord:
    record_id: UUID
    kind: MemoryKind
    content: str
    sources: tuple[EvidenceReference, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.record_id, UUID) or not isinstance(self.kind, MemoryKind):
            raise TypeError("record_id and kind must use their declared types")
        validate_text(self.content, field="memory content")
        if not isinstance(self.sources, tuple) or not self.sources:
            raise ValueError("memory records require a nonempty source tuple")
        if not all(isinstance(source, EvidenceReference) for source in self.sources):
            raise TypeError("sources must contain EvidenceReference values")


class MemoryStoreError(Exception):
    """Persistence failure; implementations must not report a successful write."""


class MemoryStore(Protocol):
    def put(self, record: MemoryRecord) -> None:
        """Store a record or raise MemoryStoreError; no M2-A record store yet."""
        ...

    def get(self, record_id: UUID) -> MemoryRecord | None:
        ...
