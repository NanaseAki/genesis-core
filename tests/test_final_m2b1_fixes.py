"""Final scoped regressions: ASCII Git decisions and independent diagnostics."""
from copy import deepcopy
import http.client
import logging
from pathlib import Path
import subprocess
import tempfile
import traceback
import unittest
from unittest.mock import patch
from uuid import UUID

from genesis._git_config import bare_flag
from genesis.core import GenesisCore, TurnGenerationError, UserInput
from genesis.data_paths import data_root_path
from genesis.models.contracts import Message, ModelRequest, Role
from genesis.models.ollama import OllamaError, OllamaFailure, OllamaModel, _failure
from genesis.storage.sqlite import SQLiteStore

MARKER = "SYNTHETIC_SHARED_PRIVATE_7039"
REQUEST = ModelRequest(UUID(int=1), UUID(int=2), UUID(int=3), (Message(Role.USER, MARKER),))


def rich_timeout() -> TimeoutError:
    error = TimeoutError(110, MARKER, MARKER, 10060, MARKER)
    error.add_note(MARKER)
    error.extra_payload = {"synthetic": [MARKER]}
    try:
        raise error
    except TimeoutError:
        pass
    error.__cause__ = RuntimeError(MARKER)
    error.__context__ = ValueError(MARKER)
    return error


def diagnostic_state(error: BaseException) -> tuple:
    return (error.args, deepcopy(error.__dict__), error.__cause__, error.__context__,
            tuple(getattr(error, field, None) for field in
                  ("errno", "winerror", "strerror", "filename", "filename2")))


def assert_safe(test: unittest.TestCase, error: BaseException, original: BaseException) -> None:
    test.assertNotIn(MARKER, "".join(traceback.format_exception(error)))
    record = logging.LogRecord("synthetic", logging.ERROR, __file__, 1, "safe failure", (),
                               (type(error), error, error.__traceback__))
    test.assertNotIn(MARKER, logging.Formatter().format(record))
    pending = [error]; seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        test.assertIsNot(current, original)
        values = [str(current), repr(current.args), repr(current.__dict__),
                  repr(getattr(current, "__notes__", None))]
        values += [repr(getattr(current, field, None)) for field in
                   ("line", "doc", "object", "partial", "errno", "strerror", "filename", "filename2")]
        for value in values:
            test.assertNotIn(MARKER, value)
        pending.extend(related for related in (current.__cause__, current.__context__) if related is not None)


def capture_failure(model: OllamaModel) -> OllamaError:
    # assertRaises strips the outer traceback when its context exits. Retain
    # the actual traceback here so the logging assertions exercise all frames.
    try:
        model.generate(REQUEST)
    except OllamaError as error:
        return error
    raise AssertionError("expected model failure")


class FinalGitBooleanTests(unittest.TestCase):
    def check_config(self, text: str, expected: bool | None) -> None:
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder) / "synthetic.git"
            subprocess.run(["git", "init", "--bare", "--quiet", str(repo)],
                           check=True, capture_output=True, timeout=10)
            (repo / "config").write_bytes(text.encode("utf-8"))
            # Capture bytes: Git's UTF-8 diagnostics must not depend on a
            # Windows machine's default console/ANSI encoding.
            result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--is-bare-repository"],
                                    capture_output=True, timeout=10)
            if expected is None:
                self.assertNotEqual(result.returncode, 0)
            else:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), str(expected).lower().encode("ascii"))
            root = repo / "synthetic-data"
            with patch("subprocess.Popen", side_effect=AssertionError("no runtime Git")):
                if expected is False:
                    self.assertIs(bare_flag(text), False)
                    self.assertEqual(data_root_path(root), root.resolve())
                else:
                    with self.assertRaises(ValueError):
                        with SQLiteStore(root):
                            pass
                    with self.assertRaises(ValueError):
                        data_root_path(root)
                    self.assertFalse(root.exists(), "rejected configuration must not create a database root")
                    if expected is True:
                        self.assertIs(bare_flag(text), True)

    def test_three_unicode_booleans_cannot_create_database_in_git_metadata(self) -> None:
        for value in ("fal\u017fe", "0\u212a", "-0\u212a"):
            with self.subTest(value=ascii(value)):
                self.check_config("[core]\nbare=" + value + "\n", None)

    def test_ascii_case_and_numeric_forms_match_actual_git(self) -> None:
        for value, expected in (("TrUe", True), ("YeS", True), ("oN", True), ("FaLsE", False),
                                ("nO", False), ("oFF", False), ("+0X2", True), ("-02K", True),
                                ("1G", True), ("-0M", False), ("0X0", False)):
            with self.subTest(value=value):
                self.check_config("[CoRe]\nBaRe=" + value + "\n", expected)

    def test_unicode_comments_and_non_decision_values_are_not_rejected(self) -> None:
        for value, expected in (("true", True), ('f"al"se', False)):
            with self.subTest(value=value):
                self.check_config('# 合成注释 ſ K\n[other "合成"]\ndescription="合成 ſ K"\n'
                                  '[core]\nbare=' + value + ' # 合法注释 ſ K\n', expected)

    def test_quotes_continuations_and_repeated_overrides_keep_git_semantics(self) -> None:
        self.check_config('[CORE]\nbare=0\n[core]\nbare=0"X"2\n[CoRe]\nBARE=fa\\\nlse\n', False)
        self.check_config('[core]\nbare=OFF\n[CORE]\nBARE="ON"\n', True)

    def test_non_ascii_names_and_earlier_invalid_values_fail_closed(self) -> None:
        for text in ('[co\u212ae]\nbare=false\n', '[core]\nba\u017fe=false\n',
                     '[core]\nbare=0\u212a\nbare=false\n'):
            with self.subTest(text=ascii(text)):
                self.check_config(text, None)


class FinalDiagnosticSnapshotTests(unittest.TestCase):
    def test_snapshot_creation_does_not_modify_any_original_field(self) -> None:
        original = rich_timeout()
        before, trace = diagnostic_state(original), original.__traceback__
        first = _failure(OllamaFailure.CONNECT_TIMEOUT, timeout=original)
        second = _failure(OllamaFailure.READ_TIMEOUT, timeout=original)
        self.assertEqual(diagnostic_state(original), before)
        self.assertIs(original.__traceback__, trace)
        self.assertIsNot(first.__cause__, original)
        self.assertIsNot(first.__cause__, second.__cause__)
        self.assertIsInstance(first.__cause__, TimeoutError)
        self.assertEqual(first.__cause__.args, ("connect_timeout",))
        self.assertEqual(second.__cause__.args, ("read_timeout",))
        assert_safe(self, first, original)
        assert_safe(self, second, original)

    def test_shared_timeout_reuse_cannot_change_prior_public_diagnostic(self) -> None:
        original = rich_timeout()
        before, original_trace = diagnostic_state(original), original.__traceback__
        model = OllamaModel("http://127.0.0.1:1", "synthetic-final")
        with patch.object(http.client.HTTPConnection, "connect", side_effect=original):
            first = capture_failure(model)
        class DummySocket:
            def settimeout(self, value): pass
            def close(self): pass
        def connected(connection):
            connection.sock = DummySocket()
        with patch.object(http.client.HTTPConnection, "connect", connected), patch.object(
                http.client.HTTPConnection, "request", side_effect=original):
            second = capture_failure(model)
        self.assertIsNotNone(first.__traceback__)
        self.assertIsNotNone(second.__traceback__)
        self.assertEqual((first.reason, second.reason),
                         (OllamaFailure.CONNECT_TIMEOUT, OllamaFailure.READ_TIMEOUT))
        self.assertEqual(first.__cause__.args, ("connect_timeout",))
        self.assertEqual(second.__cause__.args, ("read_timeout",))
        self.assertIsNot(first.__cause__, second.__cause__)
        self.assertEqual(diagnostic_state(original), before)
        # Raising an exception naturally prepends traceback frames. The adapter
        # must not clear the pre-existing tail or mutate diagnostic properties.
        trace = original.__traceback__
        while trace is not None and trace is not original_trace:
            trace = trace.tb_next
        self.assertIs(trace, original_trace)
        assert_safe(self, first, original)
        assert_safe(self, second, original)

    def test_core_wraps_safe_timeout_without_changing_original(self) -> None:
        original = rich_timeout()
        before = diagnostic_state(original)
        with tempfile.TemporaryDirectory() as root, SQLiteStore(root) as store:
            core = GenesisCore(store.identity, OllamaModel("http://127.0.0.1:1", "synthetic-final"), store=store)
            core.create_session(UUID(int=2))
            failure = None
            with patch.object(http.client.HTTPConnection, "connect", side_effect=original):
                try:
                    core.send(UUID(int=2), UserInput(UUID(int=3), MARKER))
                except TurnGenerationError as error:
                    failure = error
            self.assertIsInstance(failure, TurnGenerationError)
            self.assertIsNotNone(failure.__traceback__)
            self.assertIsInstance(failure.__cause__, OllamaError)
            self.assertEqual(failure.__cause__.reason, OllamaFailure.CONNECT_TIMEOUT)
            self.assertEqual(diagnostic_state(original), before)
            self.assertEqual(store.get_session(UUID(int=2)).turns, ())
            attempt, = store.attempts(UUID(int=2), UUID(int=3))
            self.assertEqual((attempt.state, attempt.error_code), ("failed", "model_error"))
            assert_safe(self, failure, original)
