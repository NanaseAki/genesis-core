"""The local Git oracle is for synthetic tests, never a runtime dependency."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from genesis.data_paths import data_root_path


class ReviewedGitRegressions(unittest.TestCase):
    def check_config(self, text: str, expected: bool | None) -> None:
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder) / "synthetic.git"
            subprocess.run(["git", "init", "--bare", "--quiet", str(repo)], check=True, capture_output=True, timeout=10)
            (repo / "config").write_text(text, encoding="utf-8")
            result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--is-bare-repository"],
                                    capture_output=True, text=True, timeout=10)
            if expected is None:
                self.assertNotEqual(result.returncode, 0)
            else:
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), str(expected).lower())
            with patch("subprocess.Popen", side_effect=AssertionError("no runtime Git")):
                if expected is False:
                    self.assertEqual(data_root_path(repo / "data"), (repo / "data").resolve())
                else:
                    with self.assertRaises(ValueError):
                        data_root_path(repo / "data")

    def test_mixed_case_repeated_sections_last_value_wins(self) -> None:
        self.check_config("[core]\nbare=false\n[CORE]\nbare=2\n", True)

    def test_reverse_override_and_interleaved_sections(self) -> None:
        self.check_config("[core]\nbare=true\n[CORE]\nbare=0\n", False)
        self.check_config("[core]\nbare=0\n[CORE]\nbare=2\n[core]\nbare=false\n", False)

    def test_segmented_quoted_boolean(self) -> None:
        for value, expected in [('f"al"se', False), ('t"ru"e', True), ('0"x"2', True), ('"f"al"se"', False)]:
            with self.subTest(value=value):
                self.check_config("[core]\nbare=" + value + "\n", expected)

    def test_repeated_case_insensitive_keys_and_valueless_override(self) -> None:
        self.check_config("[core] bare=2\nBARE=0\n", False)
        self.check_config("[core]\nbare=0\nBaRe\n", True)
        self.check_config("[core]\nbare=0\nBaRe # synthetic comment\n", None)

    def test_continuations_and_comments_follow_git(self) -> None:
        self.check_config('[core]\nbare=fa\\\nlse # synthetic\n', False)
        self.check_config('[core]\nbare=false\n# synthetic Windows path \\\n[CORE]\nbare=t"ru"e;comment\n', True)
        self.check_config('[core]\nbare=2\n# synthetic\u0085[core]\u0085bare=false\n', True)
        self.check_config('[core]\nbare=2\n# synthetic\r[core]\rbare=false\n', True)

    def test_escapes_in_other_values_do_not_hide_core(self) -> None:
        self.check_config('[other "synthetic"]\nvalue="notes\\\\path\\\"quote\\t\\b\\n#;"\n[CORE]\nbare=0x2\n', True)

    def test_invalid_earlier_value_is_not_hidden_by_valid_override(self) -> None:
        self.check_config("[core]\nbare=garbage\nbare=2\n", None)
        self.check_config('[core]\nbare="fal\\qse"\n', None)
        self.check_config('[core]\nbare=false ""\n', None)

    def test_unsupported_includes_and_missing_bare_fail_closed_without_reading_paths(self) -> None:
        for config in ('[core]\nbare=false\n[include]\npath=synthetic-not-read\n',
                       '[core]\nrepositoryformatversion=0\n',
                       '[core]\nbare=false\n[other]\nvalue="literal\nmultiline"\n'):
            with self.subTest(config=config), tempfile.TemporaryDirectory() as folder:
                repo = Path(folder) / "synthetic.git"
                subprocess.run(["git", "init", "--bare", "--quiet", str(repo)], check=True, capture_output=True, timeout=10)
                (repo / "config").write_text(config, encoding="utf-8")
                original = Path.open
                def checked_open(path, *args, **kwargs):
                    self.assertIn(path, {repo / "HEAD", repo / "config"})
                    return original(path, *args, **kwargs)
                with patch.object(Path, "open", checked_open), patch("subprocess.Popen", side_effect=AssertionError("no runtime Git")):
                    with self.assertRaises(ValueError):
                        data_root_path(repo / "data")
