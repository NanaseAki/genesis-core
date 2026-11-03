"""HTTP adapter failures and actual Windows-spawn restart persistence."""
import ipaddress
import json
import multiprocessing
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import UUID

import genesis
from genesis.adapters.synthetic_ollama import SyntheticOllamaServer, synthetic_response
from genesis.core import DuplicateInputError, GenesisCore, TurnGenerationError, UserInput
from genesis.memory import EvidenceOrigin
from genesis.models.ollama import OllamaError, OllamaFailure, OllamaModel
from genesis.storage.sqlite import SQLiteStore

SESSION, INPUT = UUID(int=202), UUID(int=203)
MODEL = "synthetic-http:v1"
TEXT = "合成持久化输入\n 原文 "


def _restart_worker(root: str, endpoint: str, pipe) -> None:
    # Fail before any non-loopback socket can connect in this fresh process.
    def guard(event, args):
        if event == "socket.connect":
            if not ipaddress.ip_address(args[1][0]).is_loopback:
                raise RuntimeError("non-loopback connection forbidden in synthetic restart")
    sys.addaudithook(guard)
    try:
        with SQLiteStore(root) as store:
            model = OllamaModel(endpoint, MODEL)
            turn = GenesisCore(store.identity, model, store=store).send(SESSION, UserInput(INPUT, TEXT))
            pipe.send({"phase": "complete", "module": genesis.__file__, "turn": turn,
                       "identity": store.identity, "day0": store.day0(), "events": store.events(SESSION)})
    except BaseException as error:
        pipe.send({"phase": "error", "type": type(error).__name__})
        raise
    finally:
        pipe.close()


class OllamaPersistenceTests(unittest.TestCase):
    def test_http_timeout_records_existing_failure_state_without_success(self) -> None:
        release = threading.Event()
        def delayed(request):
            if not release.wait(3):
                raise RuntimeError("synthetic timeout responder not released")
            return synthetic_response(request)
        with tempfile.TemporaryDirectory() as folder, SyntheticOllamaServer(delayed) as server:
            try:
                with SQLiteStore(folder) as store:
                    core = GenesisCore(store.identity, OllamaModel(server.endpoint, MODEL, read_timeout=0.1), store=store)
                    core.create_session(SESSION)
                    with self.assertRaises(TurnGenerationError) as caught:
                        core.send(SESSION, UserInput(INPUT, TEXT))
                    self.assertEqual(caught.exception.__cause__.reason, OllamaFailure.READ_TIMEOUT)
                    self.assertEqual(store.get_session(SESSION).turns, ())
                    attempt, = store.attempts(SESSION, INPUT)
                    self.assertEqual((attempt.state, attempt.error_code), ("failed", "model_error"))
            finally:
                release.set()

    def test_http_interruption_propagates_and_records_existing_interrupted_state(self) -> None:
        with tempfile.TemporaryDirectory() as folder, SQLiteStore(folder) as store:
            core = GenesisCore(store.identity, OllamaModel("http://127.0.0.1:1", MODEL), store=store)
            core.create_session(SESSION)
            with patch("http.client.HTTPConnection.connect", side_effect=KeyboardInterrupt()):
                with self.assertRaises(KeyboardInterrupt):
                    core.send(SESSION, UserInput(INPUT, TEXT))
            self.assertEqual(store.get_session(SESSION).turns, ())
            attempt, = store.attempts(SESSION, INPUT)
            self.assertEqual((attempt.state, attempt.error_code), ("interrupted", "generation_interrupted"))

    def test_adapter_errors_cannot_commit_a_successful_turn(self) -> None:
        cases = (
            (503, b"synthetic service error", OllamaFailure.SERVER_ERROR),
            (200, b"{", OllamaFailure.INVALID_JSON),
            (200, json.dumps({"model": MODEL, "done": False}).encode(), OllamaFailure.INCOMPLETE),
            (200, json.dumps({"model": "wrong"}).encode(), OllamaFailure.MODEL_MISMATCH),
        )
        for status, body, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as folder:
                with SyntheticOllamaServer(lambda request: (status, {}, body)) as server, SQLiteStore(folder) as store:
                    core = GenesisCore(store.identity, OllamaModel(server.endpoint, MODEL), store=store)
                    core.create_session(SESSION)
                    with self.assertRaises(TurnGenerationError) as caught:
                        core.send(SESSION, UserInput(INPUT, TEXT))
                    self.assertIsInstance(caught.exception.__cause__, OllamaError)
                    self.assertEqual(caught.exception.__cause__.reason, reason)
                    self.assertEqual(store.get_session(SESSION).turns, ())
                    attempt, = store.attempts(SESSION, INPUT)
                    self.assertEqual((attempt.state, attempt.error_code), ("failed", "model_error"))
                    self.assertEqual(store._connection.execute("SELECT revision FROM sessions").fetchone()[0], 0)
                    self.assertEqual(store._connection.execute("SELECT count(*) FROM turns").fetchone()[0], 0)
                    self.assertEqual(len(server.requests), 1)
                    with self.assertRaises(DuplicateInputError):
                        core.send(SESSION, UserInput(INPUT, TEXT + "changed"))
                    self.assertEqual(len(server.requests), 1)

    def test_failed_http_attempt_requires_explicit_same_content_retry(self) -> None:
        calls = 0

        def responder(request):
            nonlocal calls
            calls += 1
            return (500, {}, b"synthetic failure") if calls == 1 else synthetic_response(request)

        with tempfile.TemporaryDirectory() as folder, SyntheticOllamaServer(responder) as server, SQLiteStore(folder) as store:
            core = GenesisCore(store.identity, OllamaModel(server.endpoint, MODEL), store=store)
            core.create_session(SESSION)
            with self.assertRaises(TurnGenerationError):
                core.send(SESSION, UserInput(INPUT, TEXT))
            self.assertEqual(calls, 1)
            turn = core.send(SESSION, UserInput(INPUT, TEXT))
            self.assertEqual(calls, 2)
            self.assertEqual(core.send(SESSION, UserInput(INPUT, TEXT)), turn)
            self.assertEqual(calls, 2)
            self.assertEqual([a.state for a in store.attempts(SESSION, INPUT)], ["failed", "committed"])

    def test_full_core_history_is_sent_without_database_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as folder, SQLiteStore(folder) as store:
            def responder(request):
                self.assertFalse(store._connection.in_transaction)
                return synthetic_response(request)
            with SyntheticOllamaServer(responder) as server:
                core = GenesisCore(store.identity, OllamaModel(server.endpoint, MODEL), store=store)
                core.create_session(SESSION)
                first = core.send(SESSION, UserInput(INPUT, TEXT))
                core.send(SESSION, UserInput(UUID(int=204), "第二轮合成输入"))
                self.assertEqual(server.requests[1]["messages"], [
                    {"role": "user", "content": TEXT}, {"role": "assistant", "content": first.assistant.content},
                    {"role": "user", "content": "第二轮合成输入"}])

    def test_committed_http_result_replays_in_fresh_process_even_with_service_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with SyntheticOllamaServer() as server, SQLiteStore(folder) as store:
                core = GenesisCore(store.identity, OllamaModel(server.endpoint, MODEL), store=store)
                core.create_session(SESSION)
                turn = core.send(SESSION, UserInput(INPUT, TEXT))
                expected = {"turn": turn, "identity": store.identity, "day0": store.day0(), "events": store.events(SESSION)}
                endpoint = server.endpoint
                self.assertEqual(len(server.requests), 1)
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe()
            process = context.Process(target=_restart_worker, args=(folder, endpoint, child))
            try:
                process.start(); child.close()
                self.assertTrue(parent.poll(20), "restart worker timed out")
                result = parent.recv()
                self.assertEqual(result["phase"], "complete", result)
                self.assertEqual(Path(result["module"]).resolve(), Path(genesis.__file__).resolve())
                for key, value in expected.items():
                    self.assertEqual(result[key], value, key)
                process.join(20)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
                self.assertEqual(len(server.requests), 1)
            finally:
                child.close(); parent.close()
                if process.pid is not None:
                    if process.is_alive():
                        process.terminate()
                    process.join(5)
                    if process.is_alive():
                        process.kill(); process.join(5)
                    self.assertFalse(process.is_alive(), "restart child cleanup failed")
                    process.close()

    def test_model_claimed_provenance_never_becomes_user_evidence(self) -> None:
        def forged(request):
            data = {"model": MODEL, "done": True, "done_reason": "stop", "origin": "user_input",
                    "root": "pretend-user-root", "message": {"role": "assistant", "content": "合成模型自称喜欢绘画"}}
            return 200, {}, json.dumps(data).encode()
        with tempfile.TemporaryDirectory() as folder, SyntheticOllamaServer(forged) as server, SQLiteStore(folder) as store:
            core = GenesisCore(store.identity, OllamaModel(server.endpoint, MODEL), store=store)
            core.create_session(SESSION); core.send(SESSION, UserInput(INPUT, TEXT))
            events = store.events(SESSION)
            user, model = events
            self.assertEqual(user.reference.origin, EvidenceOrigin.USER_INPUT)
            self.assertEqual(model.reference.origin, EvidenceOrigin.MODEL_OUTPUT)
            self.assertEqual(model.reference.root_event_id, model.reference.event_id)
            self.assertNotEqual(model.reference.root_event_id, user.reference.event_id)
