"""Default-deny action policy. It grants no external capabilities."""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class Action(str, Enum):
    RESPOND = "respond"
    WRITE_MEMORY = "write_memory"
    APPLY_GROWTH = "apply_growth"
    EXTERNAL_ACTION = "external_action"
    CLOUD_INFERENCE = "cloud_inference"


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str


class Policy(Protocol):
    def authorize(self, action: Action) -> PolicyDecision:
        ...


@dataclass(frozen=True)
class DefaultPolicy:
    """Only ordinary replies are enabled in M1; there is no grant mutator."""

    def authorize(self, action: Action) -> PolicyDecision:
        if action is Action.RESPOND:
            return PolicyDecision(True, "m1_reply_only")
        return PolicyDecision(False, "not_enabled_in_m1")
