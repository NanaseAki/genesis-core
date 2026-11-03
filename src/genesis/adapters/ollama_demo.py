"""Fixed synthetic data only; real HTTP access requires explicit opt-in."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from uuid import UUID

from genesis.adapters.synthetic_ollama import SyntheticOllamaServer
from genesis.core import GenesisCore, UserInput
from genesis.models.ollama import OllamaModel
from genesis.storage.sqlite import SQLiteStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Synthetic Ollama protocol demo, not a real companion")
    parser.add_argument("--real-service", action="store_true", help="Explicitly enable an existing service; no model downloads")
    parser.add_argument("--endpoint", help="Explicit loopback HTTP IP and port; requires --real-service")
    parser.add_argument("--model", help="Exact response model name; requires --real-service")
    args = parser.parse_args()
    if args.real_service != bool(args.endpoint and args.model) or (
            not args.real_service and (args.endpoint or args.model)):
        parser.error("real access requires --real-service, --endpoint and --model together")
    with ExitStack() as stack:
        folder = stack.enter_context(tempfile.TemporaryDirectory(prefix="genesis-http-synthetic-"))
        server = None if args.real_service else stack.enter_context(SyntheticOllamaServer())
        endpoint = args.endpoint if args.real_service else server.endpoint
        name = args.model if args.real_service else "synthetic-http-v1"
        if args.real_service:
            print("Explicit service access: loopback does NOT verify server inference is local.")
        else:
            print("SYNTHETIC HTTP ONLY: no Ollama, no LLM, temporary synthetic identity.")
        model = OllamaModel(endpoint, name)
        root = Path(folder) / "data"
        session_id, input_id = UUID(int=200), UUID(int=201)
        with SQLiteStore(root) as store:
            core = GenesisCore(store.identity, model, store=store)
            core.create_session(session_id)
            first = core.send(session_id, UserInput(input_id, "合成测试你好\n第二行"))
            companion_id = store.identity.companion_id
        with SQLiteStore(root) as store:
            replay = GenesisCore(store.identity, model, store=store).send(
                session_id, UserInput(input_id, "合成测试你好\n第二行"))
            if replay != first or store.identity.companion_id != companion_id:
                raise RuntimeError("synthetic persistent replay changed")
        # ASCII JSON escaping also works when a Windows CI pipe uses an ANSI
        # code page; it does not alter the HTTP or persisted Unicode content.
        print("Synthetic reply (JSON): " + json.dumps(first.assistant.content, ensure_ascii=True))
        print("Reopened SQLite: identity and committed reply unchanged.")
        if server is not None:
            if len(server.requests) != 1:
                raise RuntimeError("replay unexpectedly called HTTP")
            print("Synthetic HTTP POST count: 1; replay HTTP count: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
