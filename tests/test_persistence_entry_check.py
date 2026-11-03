"""The CI entry check must reject a fresh identity or a second model call."""
from pathlib import Path
import runpy
import unittest

verify = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools" /
                           "check_persistence_entry.py"))["verify_outputs"]
BASE = "Synthetic companion UUID: synthetic\nDay 0 SHA-256: digest\nFake response: synthetic persistence hello\n"


class PersistenceEntryCheckTests(unittest.TestCase):
    def test_replay_is_accepted(self) -> None:
        verify(BASE + "Model calls this process: 1\n", BASE + "Model calls this process: 0\n")

    def test_extra_model_execution_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            verify(BASE + "Model calls this process: 1\n", BASE + "Model calls this process: 1\n")

    def test_identity_change_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            verify(BASE + "Model calls this process: 1\n",
                   BASE.replace("synthetic\n", "different\n") + "Model calls this process: 0\n")
