"""Synthetic Git layouts and directory links; runtime discovery needs no Git."""

import base64
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from genesis.data_paths import data_root_path
from genesis.storage.sqlite import SQLiteStore


def git_init(path: Path, *, bare: bool = False) -> None:
    command = ["git", "init", "--quiet"]
    if bare:
        command.append("--bare")
    subprocess.run(command + [str(path)], check=True, capture_output=True, timeout=15)


def directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        # Windows without symlink privilege: junctions exercise the same path
        # redirection guard. No skip: a failed fallback fails the test explicitly.
        script = ("$ErrorActionPreference='Stop'; New-Item -ItemType Junction "
                  "-Path $env:GENESIS_TEST_LINK_PATH -Target $env:GENESIS_TEST_LINK_TARGET | Out-Null")
        env = dict(os.environ, GENESIS_TEST_LINK_PATH=str(link), GENESIS_TEST_LINK_TARGET=str(target))
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                        base64.b64encode(script.encode("utf-16le")).decode("ascii")],
                       env=env, check=True, capture_output=True, timeout=15)


class DataRootGuardTests(unittest.TestCase):
    def test_ordinary_checkout_and_bare_repository_roots_and_descendants_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            for bare in (False, True):
                repo = Path(folder) / ("synthetic.git" if bare else "synthetic-checkout")
                git_init(repo, bare=bare)
                for candidate in (repo, repo / "data", repo / "objects" / "nested" / "data"):
                    with self.subTest(bare=bare, candidate=candidate):
                        # Runtime checking must not launch Git or any process.
                        with patch("subprocess.Popen", side_effect=AssertionError("no runtime process")):
                            with self.assertRaises(ValueError):
                                data_root_path(candidate)
                        with self.assertRaises(ValueError):
                            SQLiteStore(candidate)
                        self.assertFalse((candidate / "genesis-synthetic.sqlite3").exists())

    def test_similar_directory_names_are_not_enough_to_claim_a_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "ordinary"
            root.mkdir(); (root / "objects").mkdir(); (root / "refs").mkdir()
            (root / "HEAD").write_text("synthetic notes", encoding="utf-8")
            (root / "config").write_text("[core]\nbare = true\n", encoding="utf-8")
            self.assertEqual(data_root_path(root / "data"), (root / "data").resolve())
            (root / "HEAD").write_text("ref: refs/heads/synthetic\n", encoding="utf-8")
            (root / "config").write_text("[core]\nbare = false\n", encoding="utf-8")
            with SQLiteStore(root / "data") as store:
                self.assertTrue(store.path.exists())

    def test_bare_packed_refs_and_quoted_case_insensitive_boolean_are_recognized(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder) / "synthetic.git"
            git_init(repo, bare=True)
            (repo / "refs" / "heads").rmdir(); (repo / "refs" / "tags").rmdir(); (repo / "refs").rmdir()
            (repo / "packed-refs").write_text("# synthetic packed refs\n", encoding="utf-8")
            for value in ('"TRUE"', 'true# synthetic comment', 'On; synthetic comment', '1', None):
                with self.subTest(value=value):
                    option = 'BARE' if value is None else 'BARE = ' + value
                    (repo / "config").write_text('[CORE]\n' + option + '\n', encoding="utf-8")
                    with self.assertRaises(ValueError):
                        data_root_path(repo / "data")

    def test_symlink_outside_repository_pointing_inside_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            for bare in (False, True):
                repo = Path(folder) / ("synthetic.git" if bare else "checkout")
                git_init(repo, bare=bare)
                link = Path(folder) / ("bare-link" if bare else "checkout-link")
                directory_link(link, repo)
                with self.assertRaises(ValueError):
                    data_root_path(link / "data")

    def test_explicit_path_inside_repository_linking_outside_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "ordinary"; target.mkdir()
            for bare in (False, True):
                repo = Path(folder) / ("synthetic.git" if bare else "checkout")
                git_init(repo, bare=bare)
                link = repo / "outbound"
                directory_link(link, target)
                with self.assertRaises(ValueError):
                    data_root_path(link / "data")
                self.assertFalse((target / "data").exists())

    def test_non_git_link_target_and_normalized_outside_path_remain_usable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); target = root / "ordinary"; target.mkdir()
            link = root / "ordinary-link"; directory_link(link, target)
            self.assertEqual(data_root_path(link / "data"), (target / "data").resolve())
            repo = root / "checkout"; git_init(repo)
            self.assertEqual(data_root_path(repo / ".." / "outside"), (root / "outside").resolve())

    def test_link_privilege_fallback_is_explicit_and_never_silently_skips(self) -> None:
        if os.name != "nt":
            with patch.object(Path, "symlink_to", side_effect=OSError("synthetic privilege restriction")):
                with self.assertRaises(OSError):
                    directory_link(Path("synthetic-link"), Path("synthetic-target"))
        else:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder); repo = root / "synthetic.git"; git_init(repo, bare=True)
                link = root / "junction"
                with patch.object(Path, "symlink_to", side_effect=OSError("synthetic privilege restriction")):
                    directory_link(link, repo)
                self.assertEqual(link.resolve(), repo.resolve())
                with self.assertRaises(ValueError):
                    data_root_path(link / "data")
