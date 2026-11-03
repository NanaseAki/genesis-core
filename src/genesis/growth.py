"""Growth proposals only. There is no growth application or learning algorithm."""

from dataclasses import dataclass, field
from typing import Literal, Protocol
from uuid import UUID

from genesis.memory import EvidenceReference, MemoryRecord, TrustedEvidenceRegistry
from genesis.text import validate_text


@dataclass(frozen=True)
class GrowthCandidate:
    candidate_id: UUID
    description: str
    sources: tuple[EvidenceReference, ...]
    kind: Literal["growth_candidate"] = field(default="growth_candidate", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, UUID):
            raise TypeError("candidate_id must be a UUID")
        validate_text(self.description, field="candidate description")
        if not isinstance(self.sources, tuple) or not self.sources:
            raise ValueError("candidates require a nonempty source tuple")
        if not all(isinstance(source, EvidenceReference) for source in self.sources):
            raise TypeError("sources must contain EvidenceReference values")

    def independent_evidence_ids(
        self, registry: TrustedEvidenceRegistry
    ) -> frozenset[UUID]:
        """Resolve evidence against the host's separately held attestations.

        Current-reference origin cannot promote a model root or erase a user
        root. Unknown events/roots are unconfirmed. This structural check is not
        a confidence score, semantic evidence validator or acceptance rule.
        """
        if not isinstance(registry, TrustedEvidenceRegistry):
            raise TypeError("host-supplied TrustedEvidenceRegistry required")
        return registry.independent_roots(self.sources)


class GrowthProposer(Protocol):
    def propose(self, records: tuple[MemoryRecord, ...]) -> tuple[GrowthCandidate, ...]:
        """Return proposals, without modifying memory, identity or permissions."""
        ...
