"""Companion identity is supplied by the host, independently of its model."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class CompanionIdentity:
    companion_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.companion_id, UUID):
            raise TypeError("companion_id must be a UUID")
