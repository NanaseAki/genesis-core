"""Numeric Git configuration regression, with a real Git oracle in tests only."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from genesis.data_paths import data_root_path


class GitBooleanTests(unittest.TestCase):
    def check_values(self, values: tuple[str, ...], expected: str) -> None:
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder) / "synthetic.git"
            subprocess.run(["git", "init", "--bare", "--quiet", str(repo)], check=True,
                           capture_output=True, timeout=15)
            for value in values:
                with self.subTest(value=value):
                    (repo / "config").write_text("[core]\nbare=" + value + "\n", encoding="utf-8")
                    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--is-bare-repository"],
                                            capture_output=True, text=True, timeout=15)
                    if expected == "invalid":
                        self.assertNotEqual(result.returncode, 0)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.strip(), expected)
                    with patch("subprocess.Popen", side_effect=AssertionError("runtime must not use Git")):
                        if expected == "false":
                            self.assertEqual(data_root_path(repo / "data"), (repo / "data").resolve())
                        else:
                            with self.assertRaises(ValueError):
                                data_root_path(repo / "data")

    def test_nonzero_numeric_booleans_reject_bare_data_roots(self) -> None:
        self.check_values(("2", "-2", "+2", "02", "0x2", "2k", "1M", "1g",
                           '"2" # synthetic', "2147483647", "-2147483648"), "true")

    def test_zero_and_false_spellings_do_not_misidentify_directories(self) -> None:
        self.check_values(("0", "00", "0x0", "-0", "+0k", "false", "NO", "off", '""'), "false")

    def test_invalid_boolean_in_git_layout_fails_explicitly(self) -> None:
        self.check_values(("08", "garbage", "2147483648", "2g", "--2"), "invalid")
