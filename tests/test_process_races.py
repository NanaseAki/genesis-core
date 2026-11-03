"""Two real spawned processes, deterministic pipe handshakes, no sleep ordering."""

import multiprocessing
from pathlib import Path
import tempfile
import unittest
from uuid import UUID

import genesis
from genesis.core import GenesisCore, UserInput
from genesis.models.fake import FakeModel
from genesis.storage.sqlite import SQLiteStore, StaleAttemptError


TIMEOUT = 20


def _worker(root: str, label: str, input_id: int, channel) -> None:
    """The controller waits for READY from BOTH workers before releasing either."""
    try:
        with SQLiteStore(root) as store:
            class ControlledFakeModel(FakeModel):
                def generate(self, request):
                    channel.send({"phase": "ready", "worker": label,
                                  "module_file": genesis.__file__,
                                  "in_transaction": store._connection.in_transaction})
                    if not channel.poll(TIMEOUT) or channel.recv() != "release":
                        raise RuntimeError("synthetic worker release timeout/protocol error")
                    return super().generate(request)
            model = ControlledFakeModel(["synthetic " + label])
            core = GenesisCore(store.identity, model, store=store)
            try:
                turn = core.send(UUID(int=2), UserInput(UUID(int=input_id), "synthetic competing input"))
                result = {"phase": "complete", "reply": turn.assistant.content}
            except StaleAttemptError:
                result = {"phase": "complete", "error": "StaleAttemptError"}
            result["model_calls"] = len(model.requests)
            channel.send(result)
    except BaseException as error:
        channel.send({"phase": "unexpected_error", "error_type": type(error).__name__})
        raise
    finally:
        channel.close()


class ProcessRaceTests(unittest.TestCase):
    def _race(self, *, same_input: bool, first_to_finish: str) -> tuple[dict, dict]:
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as root:
            with SQLiteStore(root) as store:
                GenesisCore(store.identity, FakeModel(), store=store).create_session(UUID(int=2))
                identity, day0 = store.identity, store.day0()
            workers = {}
            results = {}
            try:
                for label, input_id in (("old", 3), ("new", 3 if same_input else 4)):
                    parent, child = context.Pipe(duplex=True)
                    process = context.Process(target=_worker, args=(root, label, input_id, child))
                    workers[label] = (process, parent)
                    try:
                        process.start()
                    finally:
                        child.close()
                    self.assertTrue(parent.poll(TIMEOUT), f"{label} never became ready; exit={process.exitcode}")
                    message = parent.recv()
                    self.assertEqual(message["phase"], "ready", message)
                    self.assertFalse(message["in_transaction"])
                    # Installed-package runs must also spawn that SAME package,
                    # rather than silently injecting the repository's src tree.
                    self.assertEqual(Path(message["module_file"]).resolve(), Path(genesis.__file__).resolve())
                order = (first_to_finish, "new" if first_to_finish == "old" else "old")
                for label in order:
                    process, channel = workers[label]
                    channel.send("release")
                    self.assertTrue(channel.poll(TIMEOUT), f"{label} did not finish")
                    result = channel.recv()
                    self.assertEqual(result["phase"], "complete", result)
                    self.assertEqual(result["model_calls"], 1)
                    results[label] = result
                    process.join(TIMEOUT)
                    self.assertFalse(process.is_alive(), f"{label} did not exit")
                    self.assertEqual(process.exitcode, 0)
                with SQLiteStore(root) as store:
                    self.assertEqual(store.identity, identity)
                    self.assertEqual(store.day0(), day0)
                    snapshot = store.get_session(UUID(int=2))
                    self.assertEqual(len(snapshot.turns), 1)
                    db = store._connection
                    self.assertEqual(db.execute("SELECT revision FROM sessions").fetchone()[0], 1)
                    self.assertEqual(db.execute("SELECT count(*) FROM turns").fetchone()[0], 1)
                    self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='model_reply'").fetchone()[0], 1)
                    self.assertEqual(db.execute("SELECT count(*) FROM requests").fetchone()[0], 1 if same_input else 2)
                    self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    if same_input:
                        self.assertEqual([a.state for a in store.attempts(UUID(int=2), UUID(int=3))], ["unknown", "committed"])
                    else:
                        self.assertEqual(store.attempts(UUID(int=2), UUID(int=3))[0].state, "committed")
                        self.assertEqual(store.attempts(UUID(int=2), UUID(int=4))[0].error_code, "stale_context")
                    self.assertEqual(snapshot.turns[0].assistant.content,
                                     "synthetic new" if same_input else "synthetic old")
                return results["old"], results["new"]
            finally:
                for process, channel in workers.values():
                    if process.pid is not None:
                        if process.is_alive():
                            process.terminate()
                        process.join(5)
                        if process.is_alive():
                            process.kill()
                            process.join(5)
                        self.assertFalse(process.is_alive(), "worker cleanup failed")
                        process.close()
                    channel.close()

    def test_same_input_old_attempt_returns_first_and_is_fenced(self) -> None:
        old, new = self._race(same_input=True, first_to_finish="old")
        self.assertEqual(old["error"], "StaleAttemptError")
        self.assertEqual(new["reply"], "synthetic new")

    def test_same_input_new_attempt_commits_first_and_old_replays_winner(self) -> None:
        old, new = self._race(same_input=True, first_to_finish="new")
        self.assertEqual(old["reply"], "synthetic new")
        self.assertEqual(new["reply"], "synthetic new")

    def test_different_inputs_reject_the_second_stale_context(self) -> None:
        old, new = self._race(same_input=False, first_to_finish="old")
        self.assertEqual(old["reply"], "synthetic old")
        self.assertEqual(new["error"], "StaleAttemptError")
