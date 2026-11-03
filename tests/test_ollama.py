"""Real loopback HTTP with synthetic responses only. Never needs Ollama."""
from dataclasses import FrozenInstanceError
import http.client
import json
import os
import socket
import threading
import unittest
from unittest.mock import patch
from uuid import UUID

from genesis.adapters.synthetic_ollama import SyntheticOllamaServer, synthetic_response
from genesis.models.contracts import Message, ModelError, ModelRequest, Role
from genesis.models.ollama import GenerationOptions, OllamaError, OllamaFailure, OllamaModel

MODEL = "合成模型:v1"
REQUEST = ModelRequest(UUID(int=1), UUID(int=2), UUID(int=3), (Message(Role.USER, "你好\n 世界 "),))


def response_data() -> dict:
    return {"model": MODEL, "done": True, "done_reason": "stop",
            "message": {"role": "assistant", "content": " 合成回答\n保留原文 "}}


def encoded(data: object) -> bytes:
    return json.dumps(data, ensure_ascii=True).encode("utf-8")


class OllamaConfigurationTests(unittest.TestCase):
    def test_external_and_hostname_endpoints_are_rejected_without_connections(self) -> None:
        with patch("socket.socket", side_effect=AssertionError("no connection")):
            for endpoint in ("http://example.invalid:11434", "http://192.168.1.2:11434",
                             "http://0.0.0.0:11434", "http://localhost:11434", "http://[::]:11434"):
                with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                    OllamaModel(endpoint, MODEL)

    def test_ambiguous_url_credentials_and_paths_are_rejected(self) -> None:
        for endpoint in ("https://127.0.0.1:11434", "http://user:secret@127.0.0.1:11434",
                         "http://127.0.0.1:11434/api/chat", "http://127.0.0.1:11434?x=1",
                         "http://127.0.0.1:11434#x", "http://127.0.0.1", "http://127.0.0.1:0",
                         "http://127.0.0.1:11434?", "http://127.0.0.1:11434#",
                         "http://127.0.0.1:65536", "http://127.0.0.1:abc",
                         "http://127.0.0.1:11434\n", "http://[::1%zone]:11434"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                OllamaModel(endpoint, MODEL)

    def test_literal_ipv4_and_ipv6_loopback_origins_are_accepted_without_io(self) -> None:
        for endpoint in ("http://127.0.0.1:11434", "http://[::1]:11434/"):
            self.assertEqual(OllamaModel(endpoint, MODEL).model_id, MODEL)

    def test_invalid_timeouts_and_limits_are_rejected(self) -> None:
        for key in ("connect_timeout", "read_timeout"):
            for value in (0, -1, True, None, float("inf"), float("nan"), 10**1000):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    OllamaModel("http://127.0.0.1:1", MODEL, **{key: value})
        for value in (True, 0, -1, 1.5, 16777217):
            with self.assertRaises(ValueError):
                OllamaModel("http://127.0.0.1:1", MODEL, max_response_bytes=value)

    def test_generation_options_are_explicit_typed_and_immutable(self) -> None:
        for kwargs in ({"temperature": True}, {"temperature": -1}, {"temperature": float("nan")}, {"temperature": 10**1000},
                       {"seed": True}, {"seed": 2**31}, {"num_predict": -1}, {"num_predict": 1.5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                GenerationOptions(**kwargs)
        with self.assertRaises(FrozenInstanceError):
            GenerationOptions().seed = 2
        with self.assertRaises(TypeError):
            OllamaModel("http://127.0.0.1:1", MODEL, options={"arbitrary": True})

    def test_invalid_model_ids_are_not_coerced(self) -> None:
        for value in (None, True, 7, "", " ", "bad\x00id", "\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(ModelError) as caught:
                OllamaModel("http://127.0.0.1:1", value)
            self.assertEqual(str(caught.exception), "invalid_model_id")


class OllamaHTTPTests(unittest.TestCase):
    def assert_response_error(self, body: bytes, reason: OllamaFailure, *, status: int = 200,
                              headers: dict | None = None, limit: int = 1024 * 1024) -> OllamaError:
        with SyntheticOllamaServer(lambda request: (status, headers or {}, body)) as server:
            with self.assertRaises(OllamaError) as caught:
                OllamaModel(server.endpoint, MODEL, max_response_bytes=limit).generate(REQUEST)
            self.assertEqual(caught.exception.reason, reason)
            self.assertEqual(len(server.requests), 1)  # No automatic network retry.
        return caught.exception

    def test_chinese_newlines_and_explicit_nonstreaming_request(self) -> None:
        with SyntheticOllamaServer() as server:
            model = OllamaModel(server.endpoint, MODEL, options=GenerationOptions(0.2, 17, 64))
            reply = model.generate(REQUEST)
            self.assertEqual(reply.content, "合成 HTTP 回复：你好\n 世界 ")
            self.assertEqual(server.requests, [{"model": MODEL, "messages": [{"role": "user", "content": "你好\n 世界 "}],
                                               "stream": False, "options": {"temperature": 0.2, "seed": 17, "num_predict": 64}}])
            self.assertNotIn(str(REQUEST.companion_id), json.dumps(server.requests))

    def test_complete_history_is_forwarded_in_order(self) -> None:
        messages = (Message(Role.USER, "第一问"), Message(Role.ASSISTANT, "第一答"), Message(Role.USER, "第二问"))
        with SyntheticOllamaServer() as server:
            OllamaModel(server.endpoint, MODEL).generate(ModelRequest(UUID(int=1), UUID(int=2), UUID(int=4), messages))
            self.assertEqual(server.requests[0]["messages"],
                             [{"role": message.role.value, "content": message.content} for message in messages])

    def test_reply_text_is_not_normalized(self) -> None:
        with SyntheticOllamaServer(lambda request: (200, {}, encoded(response_data()))) as server:
            self.assertEqual(OllamaModel(server.endpoint, MODEL).generate(REQUEST).content,
                             response_data()["message"]["content"])

    def test_wrong_http_status_is_rejected_without_body_disclosure(self) -> None:
        for status in (400, 404, 429, 500, 502, 503):
            with self.subTest(status=status):
                reason = OllamaFailure.SERVER_ERROR if status >= 500 else OllamaFailure.HTTP_ERROR
                error = self.assert_response_error(b"synthetic private error text", reason, status=status)
                self.assertEqual(error.status, status)
                self.assertNotIn("private", str(error))

    def test_redirect_is_not_followed_even_to_another_loopback_server(self) -> None:
        with SyntheticOllamaServer() as target:
            self.assert_response_error(b"", OllamaFailure.REDIRECT, status=307,
                                       headers={"Location": target.endpoint + "/api/chat"})
            self.assertEqual(target.requests, [])

    def test_environment_proxies_are_bypassed(self) -> None:
        with SyntheticOllamaServer() as proxy, SyntheticOllamaServer() as target:
            env = {name: proxy.endpoint for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")}
            env.update({"NO_PROXY": "", "no_proxy": ""})
            with patch.dict(os.environ, env):
                OllamaModel(target.endpoint, MODEL).generate(REQUEST)
            self.assertEqual(len(target.requests), 1)
            self.assertEqual(proxy.requests, [])

    def test_connection_refused_is_distinct(self) -> None:
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))  # Bound but deliberately not listening.
            endpoint = f"http://127.0.0.1:{reserved.getsockname()[1]}"
            with self.assertRaises(OllamaError) as caught:
                OllamaModel(endpoint, MODEL, connect_timeout=3).generate(REQUEST)
            self.assertEqual(caught.exception.reason, OllamaFailure.CONNECTION_REFUSED)

    def test_read_timeout_has_bounded_event_control_and_cleanup(self) -> None:
        entered, release = threading.Event(), threading.Event()

        def delayed(request: dict):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test failed to release synthetic responder")
            return synthetic_response(request)

        with SyntheticOllamaServer(delayed) as server:
            getresponse = http.client.HTTPConnection.getresponse
            def after_server_ready(connection):
                if not entered.wait(5):
                    raise RuntimeError("synthetic handler did not become ready")
                return getresponse(connection)
            try:
                with patch.object(http.client.HTTPConnection, "getresponse", after_server_ready), self.assertRaises(OllamaError) as caught:
                    OllamaModel(server.endpoint, MODEL, read_timeout=0.1).generate(REQUEST)
                self.assertTrue(entered.is_set())
                self.assertEqual(caught.exception.reason, OllamaFailure.READ_TIMEOUT)
                self.assertEqual(len(server.requests), 1)
            finally:
                release.set()

    def test_connect_timeout_reason_and_cause(self) -> None:
        error = TimeoutError("synthetic connect timeout")
        with patch.object(http.client.HTTPConnection, "connect", side_effect=error):
            with self.assertRaises(OllamaError) as caught:
                OllamaModel("http://127.0.0.1:1", MODEL).generate(REQUEST)
            self.assertEqual(caught.exception.reason, OllamaFailure.CONNECT_TIMEOUT)
            self.assertIsNot(caught.exception.__cause__, error)
            self.assertIsInstance(caught.exception.__cause__, TimeoutError)
            self.assertEqual(caught.exception.__cause__.args, ("connect_timeout",))
            self.assertEqual(error.args, ("synthetic connect timeout",))
            self.assertIsNone(caught.exception.__cause__.__cause__)
            self.assertIsNone(caught.exception.__cause__.__context__)

    def test_interrupt_and_unexpected_errors_propagate_and_close_connection(self) -> None:
        for error in (KeyboardInterrupt(), SystemExit(), RuntimeError("synthetic bug")):
            with self.subTest(error=type(error).__name__):
                with patch.object(http.client.HTTPConnection, "connect", side_effect=error), patch.object(
                        http.client.HTTPConnection, "close") as close:
                    with self.assertRaises(type(error)):
                        OllamaModel("http://127.0.0.1:1", MODEL).generate(REQUEST)
                    close.assert_called_once()

    def test_invalid_json_utf8_duplicate_keys_and_nonfinite_values(self) -> None:
        for body in (b"{", b"\xff", b'{"model":"x","model":"y"}', b'{"x":NaN}', b"{}{}"):
            with self.subTest(body=body):
                self.assert_response_error(body, OllamaFailure.INVALID_JSON)

    def test_invalid_top_level_and_message_shapes(self) -> None:
        for data in ([], None, {}, {**response_data(), "model": 7}, {**response_data(), "message": []},
                     {**response_data(), "message": {"role": "user", "content": "synthetic"}},
                     {**response_data(), "message": {"role": "assistant", "content": "synthetic", "tool_calls": [{"function": {}}]}}):
            with self.subTest(data=data):
                self.assert_response_error(encoded(data), OllamaFailure.INVALID_RESPONSE)

    def test_model_name_mismatch_is_not_silently_aliased(self) -> None:
        self.assert_response_error(encoded({**response_data(), "model": MODEL + ":latest"}), OllamaFailure.MODEL_MISMATCH)

    def test_unfinished_or_truncated_completion_is_rejected(self) -> None:
        for change in ({"done": False}, {"done": 1}, {"done_reason": "length"}, {"done_reason": None}):
            with self.subTest(change=change):
                self.assert_response_error(encoded({**response_data(), **change}), OllamaFailure.INCOMPLETE)

    def test_invalid_reply_text_is_rejected_without_repair(self) -> None:
        for content in (None, 7, "", " \n", "a\x00b", "\ud800"):
            data = response_data(); data["message"]["content"] = content
            with self.subTest(content=repr(content)):
                self.assert_response_error(encoded(data), OllamaFailure.INVALID_TEXT)

    def test_error_envelope_is_not_success(self) -> None:
        self.assert_response_error(encoded({**response_data(), "error": "synthetic error"}), OllamaFailure.SERVER_ERROR)

    def test_response_size_limit_with_and_without_content_length(self) -> None:
        for headers in ({}, {"X-No-Length": "1"}):
            self.assert_response_error(b"x" * 65, OllamaFailure.TOO_LARGE, limit=64, headers=headers)

    def test_truncated_body_is_not_success(self) -> None:
        body = encoded(response_data())
        self.assert_response_error(body, OllamaFailure.INCOMPLETE, headers={"Content-Length": str(len(body) + 10)})

    def test_invalid_content_type_compression_and_framing(self) -> None:
        for headers in ({"Content-Type": "application/x-ndjson"}, {"Content-Encoding": "gzip"},
                        {"Content-Length": "invalid"}, {"Transfer-Encoding": "unknown"}):
            with self.subTest(headers=headers):
                self.assert_response_error(encoded(response_data()), OllamaFailure.INVALID_RESPONSE, headers=headers)

    def test_chunked_reply_is_supported_and_bounded(self) -> None:
        body = encoded(response_data())
        chunked = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
        with SyntheticOllamaServer(lambda request: (200, {"Transfer-Encoding": "chunked"}, chunked)) as server:
            self.assertEqual(OllamaModel(server.endpoint, MODEL).generate(REQUEST).content,
                             response_data()["message"]["content"])

    def test_connection_reset_is_expected_failure(self) -> None:
        with patch.object(http.client.HTTPConnection, "getresponse", side_effect=ConnectionResetError()):
            with SyntheticOllamaServer() as server:
                with self.assertRaises(OllamaError) as caught:
                    OllamaModel(server.endpoint, MODEL).generate(REQUEST)
                self.assertEqual(caught.exception.reason, OllamaFailure.CONNECTION_ERROR)
