"""Fixed synthetic input only. Run twice with the same external data root."""

import argparse
from uuid import UUID

from genesis.core import GenesisCore, SessionNotFoundError, UserInput
from genesis.models.fake import FakeModel
from genesis.storage.sqlite import SQLiteStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Synthetic SQLite restart demo; never starts a real companion.")
    parser.add_argument("--data-root", required=True, help="Absolute synthetic data directory outside Git repositories")
    args = parser.parse_args()
    with SQLiteStore(args.data_root) as store:
        model = FakeModel()
        core = GenesisCore(store.identity, model, store=store)
        session_id = UUID(int=2)
        try:
            core.get_session(session_id)
        except SessionNotFoundError:
            core.create_session(session_id)
        reply = core.send(session_id, UserInput(UUID(int=3), "synthetic persistence hello"))
        print(f"Synthetic companion UUID: {store.identity.companion_id}")
        print(f"Day 0 SHA-256: {store.day0()[1]}")
        print(reply.assistant.content)
        print(f"Model calls this process: {len(model.requests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
