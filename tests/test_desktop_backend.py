"""Backend selection tests use only FakeModel and temporary loopback fixtures."""

from dataclasses import FrozenInstanceError
import json
import socket
import threading
import unittest
from unittest.mock import patch
from uuid import UUID

from genesis.adapters.desktop_backend import BackendConfig, backend_context
from genesis.adapters.desktop_worker import ChatWorker
from genesis.adapters.synthetic_ollama import SyntheticOllamaServer, synthetic_response
from genesis.core import GenesisCore, TurnGenerationError, UserInput
from genesis.identity import CompanionIdentity
from genesis.models.contracts import ModelError
from genesis.models.fake import FakeModel
from genesis.models.ollama import GenerationOptions, OllamaError, OllamaFailure, OllamaModel


class DesktopBackendTests(unittest.TestCase):
    def core(self, model):
        core = GenesisCore(CompanionIdentity(UUID(int=1201)), model)
        session = core.create_session(UUID(int=1202))
        return core, session.session_id

    def test_default_fake_never_creates_socket_and_does_not_change_config(self):
        with patch.object(socket, "socket", side_effect=AssertionError("unexpected socket")):
            config = BackendConfig()
            with backend_context(config) as model:
                self.assertIsInstance(model, FakeModel)
                core, session_id = self.core(model)
                reply = core.send(session_id, UserInput(UUID(int=1), "合成离线测试"))
                self.assertEqual(reply.assistant.content, "Fake response: 合成离线测试")
        with self.assertRaises(FrozenInstanceError):
            config.mode = "ollama"
        self.assertEqual(config.mode, "fake")
        self.assertFalse(config.local_service_verified)

    def test_real_config_and_context_entry_do_not_connect_or_load(self):
        with patch.object(socket, "socket", side_effect=AssertionError("unexpected socket")):
            config = BackendConfig(mode="ollama", local_service_verified=True)
            with backend_context(config) as model:
                self.assertIsInstance(model, OllamaModel)
                self.assertEqual(model.endpoint, "http://127.0.0.1:11434")
                self.assertEqual(model.model_id, "qwen2.5:7b-instruct-q4_K_M")
                self.assertEqual(model.options, GenerationOptions(temperature=0, seed=0, num_predict=256))
                self.assertEqual((model.connect_timeout, model.read_timeout), (5.0, 60.0))

    def test_real_mode_requires_explicit_boolean_acknowledgment_without_io(self):
        with patch.object(socket, "socket", side_effect=AssertionError("unexpected socket")):
            with self.assertRaises(ValueError):
                BackendConfig(mode="ollama")
            for invalid in (1, "true", None):
                with self.subTest(invalid=invalid), self.assertRaises(TypeError):
                    BackendConfig(mode="ollama", local_service_verified=invalid)
            with self.assertRaises(ValueError):
                BackendConfig(mode="unknown")
            with self.assertRaises(TypeError):
                with backend_context("ollama"):
                    self.fail("invalid configuration entered")

    def test_remote_or_ambiguous_endpoint_rejected_before_traffic(self):
        endpoints = ("https://127.0.0.1:11434", "http://localhost:11434",
                     "http://192.0.2.1:11434", "http://127.0.0.1:11434/api/chat",
                     "http://user@127.0.0.1:11434", "http://127.0.0.1:11434?x=1")
        with patch.object(socket, "socket", side_effect=AssertionError("unexpected socket")):
            for endpoint in endpoints:
                with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                    BackendConfig(mode="ollama", endpoint=endpoint, local_service_verified=True)
            for invalid in ("", " \t", "synthetic\x00model", "\ud800"):
                with self.subTest(model=repr(invalid)), self.assertRaises(ModelError):
                    BackendConfig(mode="ollama", model=invalid, local_service_verified=True)

    def test_explicit_real_mode_uses_configured_model_and_exact_multiturn_unicode(self):
        with SyntheticOllamaServer() as server:
            config = BackendConfig(mode="ollama", endpoint=server.endpoint,
                                   model="synthetic-selected-v1", local_service_verified=True)
            with backend_context(config) as model:
                self.assertEqual(server.requests, [])
                core, session_id = self.core(model)
                first = UserInput(UUID(int=21), "  合成测试，保留空格\n第二行  ")
                first_reply = core.send(session_id, first)
                second = UserInput(UUID(int=22), "合成后续问题：上一句是什么？")
                core.send(session_id, second)
                # A replay must not produce another generation request.
                self.assertEqual(core.send(session_id, first), first_reply)
            self.assertEqual(len(server.requests), 2)
            self.assertEqual(server.requests[0]["model"], "synthetic-selected-v1")
            self.assertEqual(server.requests[0]["messages"], [{"role": "user", "content": first.content}])
            self.assertEqual(server.requests[1]["messages"], [
                {"role": "user", "content": first.content},
                {"role": "assistant", "content": first_reply.assistant.content},
                {"role": "user", "content": second.content},
            ])
            self.assertIs(server.requests[0]["stream"], False)
            self.assertEqual(server.requests[0]["options"], {"temperature": 0, "seed": 0, "num_predict": 256})

    def test_missing_model_error_is_not_archived_or_retried_or_leaked(self):
        marker = "synthetic-private-error-body"

        def missing_model(request):
            return 404, {}, json.dumps({"error": marker}).encode()

        with SyntheticOllamaServer(missing_model) as server:
            config = BackendConfig(mode="ollama", endpoint=server.endpoint,
                                   model="synthetic-missing", local_service_verified=True)
            with backend_context(config) as model:
                core, session_id = self.core(model)
                with self.assertRaises(TurnGenerationError) as caught:
                    core.send(session_id, UserInput(UUID(int=31), "合成缺失模型测试"))
                self.assertIsInstance(caught.exception.__cause__, OllamaError)
                self.assertEqual(caught.exception.__cause__.reason, OllamaFailure.HTTP_ERROR)
                self.assertEqual(caught.exception.__cause__.status, 404)
                self.assertNotIn(marker, repr(caught.exception.__cause__))
                self.assertEqual(core.get_session(session_id).turns, ())
            self.assertEqual(len(server.requests), 1)

    def test_length_truncation_and_model_mismatch_are_strict_failures(self):
        cases = (("length", False, OllamaFailure.INCOMPLETE),
                 ("stop", True, OllamaFailure.MODEL_MISMATCH))
        for done_reason, mismatch, expected in cases:
            with self.subTest(done_reason=done_reason, mismatch=mismatch):
                def responder(request):
                    response = {"model": "different-synthetic-model" if mismatch else request["model"],
                                "message": {"role": "assistant", "content": "合成未发布回复"},
                                "done": True, "done_reason": done_reason}
                    return 200, {}, json.dumps(response).encode()

                with SyntheticOllamaServer(responder) as server:
                    config = BackendConfig(mode="ollama", endpoint=server.endpoint,
                                           model="synthetic-strict", local_service_verified=True)
                    with backend_context(config) as model:
                        core, session_id = self.core(model)
                        with self.assertRaises(TurnGenerationError) as caught:
                            core.send(session_id, UserInput(UUID(int=41), "合成严格响应测试"))
                        self.assertEqual(caught.exception.__cause__.reason, expected)
                        self.assertEqual(core.get_session(session_id).turns, ())
                    self.assertEqual(len(server.requests), 1)

    def test_real_mode_does_not_follow_redirect_or_read_proxy_configuration(self):
        with SyntheticOllamaServer() as trap:
            def redirect(request):
                return 302, {"Location": trap.endpoint + "/api/chat"}, b"synthetic redirect"

            with SyntheticOllamaServer(redirect) as server:
                with patch.dict("os.environ", {"HTTP_PROXY": trap.endpoint, "HTTPS_PROXY": trap.endpoint}):
                    config = BackendConfig(mode="ollama", endpoint=server.endpoint,
                                           model="synthetic-redirect", local_service_verified=True)
                    with backend_context(config) as model:
                        core, session_id = self.core(model)
                        with self.assertRaises(TurnGenerationError) as caught:
                            core.send(session_id, UserInput(UUID(int=51), "合成重定向测试"))
                        self.assertEqual(caught.exception.__cause__.reason, OllamaFailure.REDIRECT)
                self.assertEqual(len(server.requests), 1)
            self.assertEqual(trap.requests, [])

    def test_synthetic_resource_lifetime_is_owned_by_worker_thread(self):
        lifecycle = []
        servers = []

        class ObservedServer(SyntheticOllamaServer):
            def __init__(self):
                lifecycle.append(("create", threading.get_ident()))
                super().__init__(synthetic_response)
                servers.append(self)

            def __enter__(self):
                lifecycle.append(("enter", threading.get_ident()))
                return super().__enter__()

            def __exit__(self, *exc):
                lifecycle.append(("exit", threading.get_ident()))
                return super().__exit__(*exc)

        with patch("genesis.adapters.desktop_backend.SyntheticOllamaServer", ObservedServer):
            worker = ChatWorker(lambda: backend_context(BackendConfig(mode="synthetic-http")))
            try:
                self.assertTrue(worker.wait_for_events(5))
                self.assertEqual([e.kind for e in worker.poll_events()], ["ready"])
                worker.submit("合成资源测试")
                self.assertTrue(worker.wait_for_events(5))
                events = worker.poll_events()
                self.assertEqual([e.kind for e in events], ["succeeded"])
                self.assertEqual(events[0].turn.model_id, "synthetic-http-v1")
            finally:
                worker.close()
                self.assertTrue(worker.join(5))
                worker.poll_events()
        self.assertEqual([entry[0] for entry in lifecycle], ["create", "enter", "exit"])
        self.assertEqual(len({entry[1] for entry in lifecycle}), 1)
        self.assertNotEqual(lifecycle[0][1], threading.get_ident())
        self.assertFalse(servers[0]._thread.is_alive())
        self.assertEqual(servers[0]._server.fileno(), -1)


if __name__ == "__main__":
    unittest.main()
