"""Real child-process exits with synthetic data, including an open write tx."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CHILD = r'''
import json, os, sys
from uuid import UUID
sys.path.insert(0, sys.argv[1])
from genesis.core import GenesisCore, SessionNotFoundError, UserInput
from genesis.models.fake import FakeModel
from genesis.storage.sqlite import SQLiteStore
root, action = sys.argv[2:4]
with SQLiteStore(root) as store:
    print(json.dumps({'identity':str(store.identity.companion_id),'day0':store.day0()[1]}),flush=True)
    model = FakeModel([] if action == 'replay' else ['synthetic persisted reply'])
    if action == 'generation_exit':
        def exit_generate(request): os._exit(71)
        model.generate = exit_generate
    core = GenesisCore(store.identity, model, store=store)
    sid, iid = UUID(int=2), UUID(int=3)
    try: core.get_session(sid)
    except SessionNotFoundError: core.create_session(sid)
    count_before = len(core.get_session(sid).turns)
    if action in ('before_commit_exit','after_commit_exit'):
        original_commit = store._commit
        def exit_commit():
            publishing = store._connection.execute('SELECT count(*) FROM turns').fetchone()[0] == 1
            if publishing and action == 'before_commit_exit': os._exit(72)
            original_commit()
            if publishing and action == 'after_commit_exit': os._exit(73)
        store._commit = exit_commit
    turn = core.send(sid, UserInput(iid, 'synthetic process input'))
    print(json.dumps({'reply':turn.assistant.content,'before':count_before,
          'after':len(core.get_session(sid).turns),'calls':len(model.requests),
          'attempts':[a.state for a in store.attempts(sid,iid)],
          'events':[(str(e.reference.event_id),e.reference.origin.value,
                     str(e.reference.root_event_id)) for e in store.events(sid)]}),flush=True)
'''


class ProcessPersistenceTests(unittest.TestCase):
    def test_source_demo_runs_twice_in_distinct_processes_with_fixed_synthetic_input(self) -> None:
        runner = Path(__file__).resolve().parents[1] / "tools" / "run_persistence_demo.py"
        with tempfile.TemporaryDirectory() as root:
            args = [sys.executable, "-B", "-I", "-S", str(runner), "--data-root", root]
            outputs = []
            for _ in range(2):
                result = subprocess.run(args, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs.append(result.stdout.splitlines())
            self.assertEqual(outputs[0][:3], outputs[1][:3])
            self.assertEqual(outputs[0][2], "Fake response: synthetic persistence hello")
            self.assertEqual(outputs[0][3], "Model calls this process: 1")
            self.assertEqual(outputs[1][3], "Model calls this process: 0")

    def child(self, root: str, action: str, expected: int = 0) -> list[dict]:
        source = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run([sys.executable, "-B", "-I", "-S", "-c", CHILD, source, root, action],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, expected, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_committed_identity_history_receipt_and_event_ids_survive_new_process(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            first = self.child(root, "commit")
            second = self.child(root, "replay")
            self.assertEqual(first[0], second[0])
            self.assertEqual(first[1]["reply"], second[1]["reply"])
            self.assertEqual(first[1]["events"], second[1]["events"])
            self.assertEqual(second[1]["before"], 1)
            self.assertEqual(second[1]["after"], 1)
            self.assertEqual(second[1]["calls"], 0)
            self.assertEqual(second[1]["attempts"], ["committed"])

    def test_process_exit_during_generation_leaves_unknown_execution_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            crashed = self.child(root, "generation_exit", 71)
            retry = self.child(root, "retry")
            self.assertEqual(crashed[0], retry[0])
            self.assertEqual((retry[1]["before"], retry[1]["after"], retry[1]["calls"]), (0, 1, 1))
            self.assertEqual(retry[1]["attempts"], ["unknown", "committed"])

    def test_process_exit_before_commit_recovers_sqlite_journal_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            crashed = self.child(root, "before_commit_exit", 72)
            retry = self.child(root, "retry")
            self.assertEqual(crashed[0], retry[0])
            self.assertEqual((retry[1]["before"], retry[1]["after"], retry[1]["calls"]), (0, 1, 1))
            self.assertEqual(retry[1]["attempts"], ["unknown", "committed"])
            self.assertEqual(len(retry[1]["events"]), 2)

    def test_process_exit_after_commit_replays_without_generation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            crashed = self.child(root, "after_commit_exit", 73)
            replay = self.child(root, "replay")
            self.assertEqual(crashed[0], replay[0])
            self.assertEqual((replay[1]["before"], replay[1]["after"], replay[1]["calls"]), (1, 1, 0))
            self.assertEqual(replay[1]["attempts"], ["committed"])
