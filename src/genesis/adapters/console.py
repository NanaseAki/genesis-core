"""Fixed synthetic demo, deliberately not a real-user chat interface."""

import argparse
from uuid import UUID

from genesis.core import GenesisCore, UserInput
from genesis.identity import CompanionIdentity
from genesis.models.fake import FakeModel


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an offline, synthetic FakeModel demo.")
    parser.parse_args()
    identity = CompanionIdentity(UUID("00000000-0000-4000-8000-000000000001"))
    core = GenesisCore(identity, FakeModel())
    session = core.create_session(UUID("00000000-0000-4000-8000-000000000002"))
    turn = core.send(session.session_id, UserInput(
        UUID("00000000-0000-4000-8000-000000000003"), "synthetic hello"
    ))
    print(turn.assistant.content)
    return 0
