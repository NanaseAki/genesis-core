"""Default demo must start its own stub, not contact an installed service."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

from genesis.adapters.ollama_demo import main


class OllamaDemoTests(unittest.TestCase):
    def test_default_demo_uses_one_synthetic_http_call_and_replays(self) -> None:
        output = StringIO()
        with patch("sys.argv", ["genesis-ollama-demo"]), redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertIn("SYNTHETIC HTTP ONLY", output.getvalue())
        self.assertIn("Synthetic HTTP POST count: 1; replay HTTP count: 0", output.getvalue())
        reply_line = next(line for line in output.getvalue().splitlines() if line.startswith("Synthetic reply (JSON): "))
        self.assertEqual(json.loads(reply_line.removeprefix("Synthetic reply (JSON): ")),
                         "合成 HTTP 回复：合成测试你好\n第二行")
        self.assertTrue(output.getvalue().isascii())

    def test_partial_real_service_configuration_is_rejected_before_io(self) -> None:
        for args in (["--real-service"], ["--endpoint", "http://127.0.0.1:11434"],
                     ["--model", "synthetic"], ["--real-service", "--model", "synthetic"]):
            with self.subTest(args=args), patch("sys.argv", ["demo", *args]), redirect_stderr(StringIO()):
                with patch("socket.socket", side_effect=AssertionError("no connection")):
                    with self.assertRaises(SystemExit) as caught:
                        main()
                    self.assertEqual(caught.exception.code, 2)

    def test_test_audit_rejects_external_connections_and_name_lookups(self) -> None:
        guard = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools" /
                                  "check_loopback_tests.py"))["guard"]
        for event, args in (("socket.connect", (None, ("203.0.113.1", 80))),
                            ("socket.getaddrinfo", ("example.invalid", 80)),
                            ("socket.gethostbyaddr", ("127.0.0.1",)),
                            ("socket.gethostbyname", ("example.invalid",)),
                            ("socket.sendto", (None, b"synthetic", ("203.0.113.1", 80)))):
            with self.subTest(event=event), self.assertRaises(RuntimeError):
                guard(event, args)
        guard("socket.connect", (None, ("127.0.0.1", 1)))
        guard("socket.connect", (None, ("::1", 1, 0, 0)))
