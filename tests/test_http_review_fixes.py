"""Wire-level regressions that fail against the reviewed M2-B1 implementation."""
from copy import deepcopy
import http.client
import json
import tempfile
import traceback
import unittest
from unittest.mock import patch
from uuid import UUID

from http_synthetic_raw import RawHTTPServer
from genesis.core import GenesisCore, TurnGenerationError, UserInput
from genesis.models.contracts import Message, ModelRequest, Role
from genesis.models.ollama import OllamaError, OllamaModel
from genesis.storage.sqlite import SQLiteStore

MODEL = "synthetic-review:v1"
MARKER = "SYNTHETIC_PRIVATE_MUST_NOT_ESCAPE_7942"
REQUEST = ModelRequest(UUID(int=1), UUID(int=2), UUID(int=3), (Message(Role.USER, MARKER),))
BODY = json.dumps({"model": MODEL, "message": {"role": "assistant", "content": "合成完整回复"},
                   "done": True, "done_reason": "stop"}, ensure_ascii=False).encode("utf-8")


def wire(body: bytes, headers: bytes = b"Transfer-Encoding: chunked\r\n", status: bytes = b"200 OK") -> bytes:
    return b"HTTP/1.0 " + status + b"\r\nContent-Type: application/json\r\n" + headers + b"\r\n" + body


def chunked(body: bytes = BODY, separator: bytes = b"\r\n", ending: bytes = b"0\r\n\r\n") -> bytes:
    return f"{len(body):x}\r\n".encode() + body + separator + ending


def assert_private(test: unittest.TestCase, error: BaseException) -> None:
    test.assertNotIn(MARKER, "".join(traceback.format_exception(error)))
    pending = [error]; seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        for value in (str(current), repr(current.args), repr(current.__dict__),
                      *(repr(getattr(current, name, None)) for name in
                        ("line", "doc", "object", "partial", "filename", "filename2", "strerror"))):
            test.assertNotIn(MARKER, value)
        for related in (current.__cause__, current.__context__):
            if related is not None:
                pending.append(related)


class ReviewedHTTPRegressions(unittest.TestCase):
    def test_bad_chunk_separator_never_commits(self) -> None:
        with tempfile.TemporaryDirectory() as folder, RawHTTPServer(wire(chunked(separator=b"XX"))) as server, SQLiteStore(folder) as store:
            core = GenesisCore(store.identity, OllamaModel(server.endpoint, MODEL), store=store)
            core.create_session(UUID(int=2))
            with self.assertRaises(TurnGenerationError):
                core.send(UUID(int=2), UserInput(UUID(int=3), MARKER))
            self.assertEqual(store.get_session(UUID(int=2)).turns, ())
            attempt, = store.attempts(UUID(int=2), UUID(int=3))
            self.assertEqual((attempt.state, attempt.error_code), ("failed", "model_error"))

    def test_missing_chunk_terminator_is_rejected(self) -> None:
        with RawHTTPServer(wire(chunked(ending=b"0\r\n"))) as server:
            with self.assertRaises(OllamaError):
                OllamaModel(server.endpoint, MODEL).generate(REQUEST)

    def test_negative_chunk_is_rejected_before_unbounded_read(self) -> None:
        class BoundedSpy:
            def __init__(self, fp):
                self.fp = fp
            def read(self, size=-1):
                if size < 0 or size > 4096:
                    raise AssertionError("unbounded/oversized response read")
                return self.fp.read(size)
            def __getattr__(self, name):
                return getattr(self.fp, name)
        original = http.client.HTTPConnection.getresponse
        def capture(connection):
            response = original(connection)
            response.fp = BoundedSpy(response.fp)
            return response
        with RawHTTPServer(wire(b"-1\r\n" + b"x" * 8192)) as server, patch.object(http.client.HTTPConnection, "getresponse", capture):
            with self.assertRaises(OllamaError):
                OllamaModel(server.endpoint, MODEL, max_response_bytes=256).generate(REQUEST)

    def test_http_error_closes_actual_response_and_stream(self) -> None:
        original = http.client.HTTPConnection.getresponse
        captured = []
        def capture(connection):
            response = original(connection)
            captured.append((response, response.fp))
            return response
        with RawHTTPServer(wire(b"abc", b"Content-Length: 3\r\n", b"503 Nope")) as server, patch.object(http.client.HTTPConnection, "getresponse", capture):
            with self.assertRaises(OllamaError) as caught:
                OllamaModel(server.endpoint, MODEL).generate(REQUEST)
            self.assertEqual(caught.exception.reason.value, "server_error")
            self.assertTrue(captured[0][0].isclosed())
            self.assertTrue(captured[0][1].closed)

    def test_full_exception_graph_and_core_traceback_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as folder, RawHTTPServer((MARKER + "\r\n\r\n").encode()) as server, SQLiteStore(folder) as store:
            core = GenesisCore(store.identity, OllamaModel(server.endpoint, MODEL), store=store)
            core.create_session(UUID(int=2))
            with self.assertRaises(TurnGenerationError) as caught:
                core.send(UUID(int=2), UserInput(UUID(int=3), MARKER))
            assert_private(self, caught.exception)

    def test_invalid_ipv6_authority_is_rejected_consistently(self) -> None:
        with self.assertRaises(ValueError):
            OllamaModel("http://[::1]garbage:1234", MODEL)
        for endpoint in ("http://[::1]:11434", "http://127.0.0.1:11434"):
            self.assertEqual(OllamaModel(endpoint, MODEL).model_id, MODEL)

    def reject(self, response: bytes, *, limit: int = 1024 * 1024) -> OllamaError:
        with RawHTTPServer(response) as server:
            with self.assertRaises(OllamaError) as caught:
                OllamaModel(server.endpoint, MODEL, max_response_bytes=limit).generate(REQUEST)
            self.assertEqual(server.requests, 1)
            assert_private(self, caught.exception)
            return caught.exception

    def test_chunk_lengths_must_be_unsigned_bounded_hex(self) -> None:
        for length in (b"-1", b"+1", b"0x10", b"1_0", b" 1", b"g", b"", b"1" * 17):
            with self.subTest(length=length):
                self.reject(wire(length + b"\r\n" + b"x" * 128))

    def test_declared_chunk_oversize_is_rejected_before_payload_read(self) -> None:
        with RawHTTPServer(wire(b"100000\r\n")) as server:
            error = None
            with self.assertRaises(OllamaError) as caught:
                OllamaModel(server.endpoint, MODEL, max_response_bytes=256).generate(REQUEST)
            error = caught.exception
            self.assertEqual(error.reason.value, "response_too_large")

    def test_cumulative_body_size_limit_applies_across_chunks(self) -> None:
        chunks = b"".join(f"{len(BODY[i:i+10]):x}\r\n".encode() + BODY[i:i+10] + b"\r\n"
                          for i in range(0, len(BODY), 10)) + b"0\r\n\r\n"
        self.assertEqual(self.reject(wire(chunks), limit=len(BODY)-1).reason.value, "response_too_large")
        with RawHTTPServer(wire(chunks)) as server:
            self.assertEqual(OllamaModel(server.endpoint, MODEL, max_response_bytes=len(BODY)).generate(REQUEST).content,
                             "合成完整回复")

    def test_truncated_length_payload_and_crlf_are_rejected(self) -> None:
        for body in (b"10", b"10\r\nshort", f"{len(BODY):x}\r\n".encode() + BODY + b"\r",
                     chunked(ending=b""), chunked(ending=b"0\r\n\r")):
            with self.subTest(size=len(body)):
                self.reject(wire(body))

    def test_extensions_support_validated_tokens_and_ascii_quoted_values(self) -> None:
        line = f"{len(BODY):X}".encode() + b';flag; name=token; quoted="semi;quote\\\"slash\\\\"\r\n'
        with RawHTTPServer(wire(line + BODY + b"\r\n0;end=yes\r\n\r\n")) as server:
            self.assertEqual(OllamaModel(server.endpoint, MODEL).generate(REQUEST).content, "合成完整回复")

    def test_invalid_or_unsupported_extensions_fail_closed(self) -> None:
        for extension in (b";", b";=x", b";name=", b';name="unterminated', b";name=(value)",
                          b';name="\xff"', b";name=x\x00"):
            with self.subTest(extension=extension):
                self.reject(wire(f"{len(BODY):x}".encode() + extension + b"\r\n" + BODY + b"\r\n0\r\n\r\n"))

    def test_chunk_control_line_and_total_overhead_are_bounded(self) -> None:
        self.reject(wire(b"1;pad=" + b"x" * 4096 + b"\r\nx\r\n0\r\n\r\n"))
        chunks = b"".join(b"1;pad=" + b"x" * 4000 + b"\r\n" + bytes([c]) + b"\r\n" for c in BODY) + b"0\r\n\r\n"
        self.assertEqual(self.reject(wire(chunks)).reason.value, "invalid_response")

    def test_trailers_and_extra_bytes_after_end_are_rejected(self) -> None:
        for body, headers in (
            (chunked(ending=b"0\r\nX-Synthetic: value\r\n\r\n"), b"Transfer-Encoding: chunked\r\n"),
            (chunked(), b"Transfer-Encoding: chunked\r\nTrailer: X-Synthetic\r\n"),
            (chunked() + b"unexpected", b"Transfer-Encoding: chunked\r\n"),
            (BODY + b"unexpected", f"Content-Length: {len(BODY)}\r\n".encode()),
        ):
            with self.subTest(headers=headers):
                self.reject(wire(body, headers))

    def test_ambiguous_and_unsupported_framing_headers_are_rejected(self) -> None:
        for headers in (b"Transfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n",
                        b"Transfer-Encoding: chunked\r\nContent-Length: 0\r\n",
                        b"Content-Length: 1\r\nContent-Length: 1\r\n",
                        b"Transfer-Encoding: gzip, chunked\r\n", b"Transfer-Encoding:\r\n",
                        b"Content-Length: -1\r\n", b"Content-Length: +1\r\n", b"Content-Length:\r\n"):
            with self.subTest(headers=headers):
                self.reject(wire(chunked(), headers))
        # The stdlib can silently discard these malformed fields. A complete
        # JSON body must not turn that loss of framing information into success.
        for headers in (b"Transfer-Encoding : chunked\r\n", b"Content-Length : 9999\r\n",
                        b"invalid-header\r\nContent-Length: 9999\r\n"):
            with self.subTest(malformed=headers):
                self.reject(wire(BODY, headers))

    def test_valid_http11_content_length_chunked_and_close_delimited(self) -> None:
        for body, headers in ((BODY, f"Content-Length: {len(BODY)}\r\n".encode()),
                              (chunked(), b"Transfer-Encoding: chunked\r\n"), (BODY, b""),
                              (chunked(), b"Transfer-Encoding: Chunked \t\r\n")):
            response = wire(body, headers).replace(b"HTTP/1.0", b"HTTP/1.1", 1)
            with self.subTest(headers=headers), RawHTTPServer(response) as server:
                self.assertEqual(OllamaModel(server.endpoint, MODEL).generate(REQUEST).content, "合成完整回复")
                self.assertIn(b"connection: close", server.request_headers.lower())

    def test_documented_ollama_nonstreaming_shape_with_metrics_and_json_newline(self) -> None:
        payload = json.loads(BODY)
        payload.update({"created_at": "2025-10-17T23:14:07Z", "total_duration": 174560334,
                        "load_duration": 101397084, "prompt_eval_count": 11,
                        "prompt_eval_duration": 13074791, "eval_count": 18, "eval_duration": 52479709})
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        for framed, headers in ((body, f"Content-Length: {len(body)}\r\n".encode()),
                                (chunked(body), b"Transfer-Encoding: chunked\r\n")):
            with self.subTest(headers=headers), RawHTTPServer(wire(framed, headers)) as server:
                self.assertEqual(OllamaModel(server.endpoint, MODEL).generate(REQUEST).content, "合成完整回复")

    def test_strict_chunked_success_is_persisted_and_reopened_without_http(self) -> None:
        with tempfile.TemporaryDirectory() as folder, RawHTTPServer(wire(chunked())) as server:
            with SQLiteStore(folder) as store:
                core = GenesisCore(store.identity, OllamaModel(server.endpoint, MODEL), store=store)
                core.create_session(UUID(int=2))
                first = core.send(UUID(int=2), UserInput(UUID(int=3), MARKER))
                identity, day0 = store.identity, store.day0()
            with SQLiteStore(folder) as store:
                replay = GenesisCore(store.identity, OllamaModel(server.endpoint, MODEL), store=store).send(
                    UUID(int=2), UserInput(UUID(int=3), MARKER))
                self.assertEqual(replay, first)
                self.assertEqual((store.identity, store.day0()), (identity, day0))
            self.assertEqual(server.requests, 1)

    def test_invalid_chunked_variants_never_publish_sqlite_history(self) -> None:
        for payload in (b"-1\r\n" + BODY, chunked(ending=b"0\r\n"), chunked() + b"extra"):
            with self.subTest(size=len(payload)), tempfile.TemporaryDirectory() as folder:
                with RawHTTPServer(wire(payload)) as server, SQLiteStore(folder) as store:
                    core = GenesisCore(store.identity, OllamaModel(server.endpoint, MODEL), store=store)
                    core.create_session(UUID(int=2))
                    with self.assertRaises(TurnGenerationError):
                        core.send(UUID(int=2), UserInput(UUID(int=3), MARKER))
                    self.assertEqual(store.get_session(UUID(int=2)).turns, ())
                    self.assertEqual(store._connection.execute("SELECT revision FROM sessions").fetchone()[0], 0)
                    self.assertEqual(store._connection.execute("SELECT count(*) FROM events WHERE kind='model_reply'").fetchone()[0], 0)
                    attempt, = store.attempts(UUID(int=2), UUID(int=3))
                    self.assertEqual((attempt.state, attempt.error_code), ("failed", "model_error"))

    def check_closed(self, response_bytes: bytes, *, error_type=None, interrupt=None, hold_open=False,
                     limit=1024*1024, expected_reason=None) -> None:
        original = http.client.HTTPConnection.getresponse
        captured = []
        def capture(connection):
            response = original(connection)
            captured.append((response, response.fp))
            if interrupt is not None:
                def interrupted(*args, **kwargs):
                    raise interrupt
                response.getheader = interrupted
            return response
        with RawHTTPServer(response_bytes, hold_open=hold_open) as server, patch.object(http.client.HTTPConnection, "getresponse", capture):
            model = OllamaModel(server.endpoint, MODEL, read_timeout=0.15, max_response_bytes=limit)
            if error_type is None:
                model.generate(REQUEST)
            else:
                with self.assertRaises(error_type) as caught:
                    model.generate(REQUEST)
                if expected_reason is not None:
                    self.assertEqual(caught.exception.reason.value, expected_reason)
            self.assertEqual(len(captured), 1)
            self.assertTrue(captured[0][0].isclosed())
            self.assertTrue(captured[0][1].closed)

    def test_actual_response_closes_after_success_or_json_failure(self) -> None:
        self.check_closed(wire(BODY, b""))
        self.check_closed(wire(b"{", b"Content-Length: 1\r\n"), error_type=OllamaError, expected_reason="invalid_json")

    def test_actual_response_closes_on_all_body_limit_paths(self) -> None:
        for body, headers in ((BODY, b""), (BODY, f"Content-Length: {len(BODY)}\r\n".encode()),
                              (chunked(), b"Transfer-Encoding: chunked\r\n")):
            with self.subTest(headers=headers):
                self.check_closed(wire(body, headers), error_type=OllamaError, limit=32, expected_reason="response_too_large")

    def test_actual_response_closes_on_keyboardinterrupt_and_systemexit(self) -> None:
        for interrupt in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(type=type(interrupt).__name__):
                self.check_closed(wire(BODY, f"Content-Length: {len(BODY)}\r\n".encode()),
                                  error_type=type(interrupt), interrupt=interrupt)

    def test_actual_response_closes_after_partial_body_timeout(self) -> None:
        self.check_closed(wire(b"partial", b"Content-Length: 100\r\n"),
                          error_type=OllamaError, hold_open=True, expected_reason="read_timeout")

    def test_success_requires_eof_not_just_a_valid_json_prefix(self) -> None:
        self.check_closed(wire(BODY, f"Content-Length: {len(BODY)}\r\n".encode()),
                          error_type=OllamaError, hold_open=True, expected_reason="read_timeout")

    def test_json_and_text_failures_expose_no_raw_exception_payloads(self) -> None:
        for body in ((MARKER + "{").encode(), (MARKER + "\xff").encode("latin1"),
                     json.dumps({"model": MODEL, "done": True, "done_reason": "stop",
                                 "message": {"role": "assistant", "content": MARKER + "\x00"}}).encode()):
            with self.subTest(size=len(body)):
                self.reject(wire(body, f"Content-Length: {len(body)}\r\n".encode()))
        self.reject(wire(MARKER.encode(), f"Content-Length: {len(MARKER)}\r\n".encode(), b"500 Server Error"))

    def test_timeout_cause_is_normalized_and_detached_without_identity_regression(self) -> None:
        """Legacy test ID retained; cause identity is now deliberately independent."""
        for timeout in (TimeoutError(MARKER), TimeoutError(110, MARKER, MARKER, 10060, MARKER)):
            with self.subTest(structured=timeout.errno is not None):
                timeout.filename = timeout.filename2 = timeout.strerror = MARKER
                timeout.extra_payload = MARKER
                timeout.add_note(MARKER)
                timeout.__cause__ = RuntimeError(MARKER)
                timeout.__context__ = ValueError(MARKER)
                before = (timeout.args, deepcopy(timeout.__dict__), timeout.__cause__, timeout.__context__,
                          timeout.errno, getattr(timeout, "winerror", None),
                          timeout.strerror, timeout.filename, timeout.filename2)
                with patch.object(http.client.HTTPConnection, "connect", side_effect=timeout):
                    with self.assertRaises(OllamaError) as caught:
                        OllamaModel("http://127.0.0.1:1", MODEL).generate(REQUEST)
                    self.assertIsNot(caught.exception.__cause__, timeout)
                    self.assertIsInstance(caught.exception.__cause__, TimeoutError)
                    self.assertEqual(caught.exception.reason.value, "connect_timeout")
                    self.assertEqual(str(caught.exception.__cause__), "connect_timeout")
                    self.assertEqual((timeout.args, timeout.__dict__, timeout.__cause__, timeout.__context__,
                                      timeout.errno, getattr(timeout, "winerror", None),
                                      timeout.strerror, timeout.filename, timeout.filename2), before)
                    self.assertIsNotNone(timeout.__traceback__)
                    assert_private(self, caught.exception)

    def test_public_error_does_not_attach_callers_ambient_exception(self) -> None:
        with RawHTTPServer((MARKER + "\r\n").encode()) as server:
            try:
                raise RuntimeError(MARKER)
            except RuntimeError:
                with self.assertRaises(OllamaError) as caught:
                    OllamaModel(server.endpoint, MODEL).generate(REQUEST)
                self.assertIsNone(caught.exception.__context__)
                assert_private(self, caught.exception)
