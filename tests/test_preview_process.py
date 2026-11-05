"""Real spawned desktop restarts; only synthetic text in temporary preview data."""

from contextlib import contextmanager, nullcontext
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import unittest

import genesis
from genesis.adapters.desktop_worker import ChatWorker
from genesis.models.fake import FakeModel
from genesis.storage.sqlite import SQLiteStore


TIMEOUT = 20
TEXT = "  synthetic preview restart\nexact original input  "
REPLY = "synthetic persisted preview reply"


def _receive(worker, expected):
    if not worker.wait_for_events(5):
        raise AssertionError("synthetic desktop event timeout")
    events = worker.poll_events()
    if [event.kind for event in events] != [expected]:
        raise AssertionError([(event.kind, event.error_code) for event in events])
    return events[0]


def _snapshot(root, session_id, input_id):
    # This connection belongs to this calling thread; the desktop connection
    # remains owned and closed exclusively by ChatWorker's thread.
    with SQLiteStore(root, data_kind="local_preview", mode="open") as store:
        recovery = store.recovery_snapshot(session_id)
        return {"session": recovery.session,
                "pending": recovery.unfinished_requests,
                "attempts": () if input_id is None else store.attempts(session_id, input_id),
                "events": store.events(session_id),
                "session_ids": store.session_ids()}


def _preview_process(root, action, session_id, input_id, channel):
    """Each invocation starts a fresh interpreter and worker-owned connection."""
    release = threading.Event()
    worker = None
    metadata = {}
    new = action in ("commit", "generation_exit", "before_commit_exit")

    class ControlledModel(FakeModel):
        def generate(self, request):
            if not release.wait(5):
                raise AssertionError("synthetic submission was not acknowledged")
            if action == "generation_exit":
                os._exit(71)
            return super().generate(request)

    model = ControlledModel([] if action == "replay" else [REPLY])

    @contextmanager
    def store_factory():
        with SQLiteStore(root, data_kind="local_preview",
                         mode="create" if new else "open") as store:
            metadata.update(identity=store.identity, day0=store.day0(),
                            data_kind=store.data_kind, filename=store.path.name,
                            application_id=store._connection.execute(
                                "PRAGMA application_id").fetchone()[0])
            if action == "before_commit_exit":
                # Force dirty pages to spill so this is a real hot rollback
                # journal, rather than just an uncommitted in-memory cache.
                store._connection.execute("PRAGMA cache_size = 1")
                store._connection.execute("PRAGMA cache_spill = ON")
                original_commit = store._commit

                def exit_before_publication():
                    if store._connection.execute("SELECT count(*) FROM turns").fetchone()[0] == 1:
                        os._exit(72)
                    original_commit()

                store._commit = exit_before_publication
            yield store

    try:
        worker = ChatWorker(lambda: nullcontext(model), store_factory=store_factory,
                            startup="new" if new else "resume",
                            resume_session_id=None if new else session_id)
        ready = _receive(worker, "ready")
        session_id = ready.session_id
        # The explicit read after ready also verifies that merely opening and
        # restoring has not fenced or otherwise changed a running attempt.
        before = _snapshot(root, session_id, input_id)
        channel.send({"phase": "ready", "pid": os.getpid(),
                      "module": genesis.__file__, "metadata": metadata,
                      "event": ready, "snapshot": before,
                      "model_calls": len(model.requests)})
        accepted = worker.submit(TEXT) if new else worker.retry(input_id)
        channel.send({"phase": "accepted", "input": accepted})
        # Send the exact accepted ID before permitting the intentional exit.
        release.set()
        succeeded = _receive(worker, "succeeded")
        worker.close()
        if not worker.join(6):
            raise AssertionError("synthetic desktop worker did not finish")
        if [event.kind for event in worker.poll_events()] != ["closed"]:
            raise AssertionError("desktop cleanup did not publish one closed event")
        after = _snapshot(root, session_id, accepted.input_id)
        channel.send({"phase": "complete", "event": succeeded,
                      "snapshot": after, "model_calls": len(model.requests),
                      "requests": model.requests})
    except BaseException as error:
        channel.send({"phase": "error", "type": type(error).__name__})
        raise
    finally:
        release.set()
        if worker is not None:
            worker.close()
            if not worker.join(6):
                raise AssertionError("synthetic desktop cleanup timeout")
        channel.close()


class PreviewProcessTests(unittest.TestCase):
    def child(self, root, action, session_id=None, input_id=None, *, expected_exit=0):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(target=_preview_process,
                                  args=(root, action, session_id, input_id, child))
        messages = []
        try:
            try:
                process.start()
            finally:
                child.close()
            phases = ("ready", "accepted", "complete") if expected_exit == 0 else ("ready", "accepted")
            for phase in phases:
                self.assertTrue(parent.poll(TIMEOUT), f"missing {phase}; exit={process.exitcode}")
                message = parent.recv()
                self.assertEqual(message["phase"], phase, message)
                messages.append(message)
            process.join(TIMEOUT)
            self.assertFalse(process.is_alive(), "synthetic preview child did not exit")
            self.assertEqual(process.exitcode, expected_exit)
            # An installed-test run must spawn the same installed package. No
            # repository src directory is added by this test or its child.
            self.assertEqual(Path(messages[0]["module"]).resolve(), Path(genesis.__file__).resolve())
            self.assertNotEqual(messages[0]["pid"], os.getpid())
            return messages
        finally:
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join(5)
                self.assertFalse(process.is_alive(), "preview process cleanup failed")
                process.close()
            parent.close()

    def assert_preview(self, ready):
        metadata = ready["metadata"]
        self.assertEqual(metadata["data_kind"], "local_preview")
        self.assertEqual(metadata["filename"], "genesis-local-preview.sqlite3")
        self.assertEqual(metadata["application_id"], 0x474C5031)
        day0 = json.loads(metadata["day0"][0])
        self.assertEqual((day0["data_kind"], day0["format_version"]), ("local_preview", 2))
        self.assertEqual(day0["companion_id"], str(metadata["identity"].companion_id))
        self.assertEqual(ready["model_calls"], 0)
        self.assertEqual(ready["event"].session_ids, (ready["event"].session_id,))

    def test_worker_success_restores_same_identity_history_and_replays_without_model(self):
        with tempfile.TemporaryDirectory() as root:
            first = self.child(root, "commit")
            self.assert_preview(first[0])
            original = first[1]["input"]
            session_id = first[0]["event"].session_id
            self.assertEqual(original.content, TEXT)
            self.assertEqual(first[0]["event"].history, ())
            self.assertEqual(first[2]["model_calls"], 1)
            self.assertEqual(first[2]["event"].turn.assistant.content, REPLY)
            second = self.child(root, "replay", session_id, original.input_id)
            self.assert_preview(second[0])
            self.assertEqual(first[0]["metadata"], second[0]["metadata"])
            self.assertEqual(second[0]["event"].session_id, session_id)
            self.assertEqual(second[0]["event"].history, first[2]["snapshot"]["session"].turns)
            self.assertEqual(second[0]["event"].pending_requests, ())
            self.assertEqual(second[0]["snapshot"], first[2]["snapshot"])
            self.assertEqual(second[1]["input"], original)
            self.assertEqual(second[2]["event"].turn, first[2]["event"].turn)
            self.assertEqual(second[2]["model_calls"], 0)
            self.assertEqual(second[2]["requests"], ())
            self.assertEqual(second[2]["snapshot"], first[2]["snapshot"])
            self.assertEqual([item.state for item in second[2]["snapshot"]["attempts"]], ["committed"])

    def test_crashed_request_stays_running_until_explicit_original_id_retry(self):
        for action, exit_code in (("generation_exit", 71), ("before_commit_exit", 72)):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as root:
                first = self.child(root, action, expected_exit=exit_code)
                self.assert_preview(first[0])
                original = first[1]["input"]
                session_id = first[0]["event"].session_id
                if action == "before_commit_exit":
                    journal = Path(root) / "genesis-local-preview.sqlite3-journal"
                    self.assertGreater(journal.stat().st_size, 512)
                    with journal.open("rb") as stream:
                        self.assertEqual(stream.read(8), bytes.fromhex("d9d505f920a163d7"))
                recovered = self.child(root, "recover", session_id, original.input_id)
                self.assert_preview(recovered[0])
                self.assertEqual(recovered[0]["metadata"], first[0]["metadata"])
                self.assertEqual(recovered[0]["event"].session_id, session_id)
                self.assertEqual(recovered[0]["event"].history, ())
                before = recovered[0]["snapshot"]
                self.assertEqual(before["session"].turns, ())
                self.assertEqual([attempt.state for attempt in before["attempts"]], ["running"])
                pending, = recovered[0]["event"].pending_requests
                self.assertEqual(pending.user_input, original)
                self.assertEqual(pending.attempts, before["attempts"])
                self.assertEqual(before["pending"], (pending,))
                self.assertEqual(len(before["events"]), 1)
                self.assertEqual(recovered[1]["input"], original)
                result = recovered[2]
                self.assertEqual(result["model_calls"], 1)
                request, = result["requests"]
                self.assertEqual((request.session_id, request.input_id), (session_id, original.input_id))
                self.assertEqual(request.messages[-1].content, original.content)
                self.assertEqual(result["event"].turn.user_input, original)
                self.assertEqual(result["event"].turn.assistant.content, REPLY)
                after = result["snapshot"]
                self.assertEqual(len(after["session"].turns), 1)
                self.assertEqual(after["pending"], ())
                self.assertEqual([attempt.state for attempt in after["attempts"]], ["unknown", "committed"])
                self.assertEqual(after["attempts"][0].attempt_id, before["attempts"][0].attempt_id)
                self.assertEqual(after["attempts"][0].error_code, "retry_superseded")
                self.assertEqual(after["events"][0], before["events"][0])
                self.assertEqual(len(after["events"]), 2)
                self.assertEqual(after["session_ids"], (session_id,))
