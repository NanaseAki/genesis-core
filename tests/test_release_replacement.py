"""Fixed-object export must ignore all Git replacement mechanisms."""
import os
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import test_release_prepare as fixture
release = fixture.release


class ReplacementSecurityTests(unittest.TestCase):
    def check_replacement(self, kind, interference=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); files = fixture.source_files(); commit = fixture.synthetic_git(root, files)
            def git(*args, data=None, raw=False):
                return subprocess.check_output(["git", "-c", "safe.directory=" + str(root), "-C", str(root),
                                                *args], input=data, timeout=15) if raw else git(*args, data=data, raw=True).decode().strip()
            if kind in ("blob", "manifest", "config"):
                path = {"blob": "README.md", "manifest": release.MANIFEST, "config": "pyproject.toml"}[kind]
                original = git("rev-parse", commit + ":" + path)
                replacement_data = (json.dumps(json.loads(files[release.MANIFEST]), indent=2).encode()
                                    if kind == "manifest" else b"SYNTHETIC_REPLACEMENT\n")
                replacement = git("hash-object", "-w", "--stdin", data=replacement_data)
            else:
                (root / "README.md").write_bytes(b"SYNTHETIC_REPLACEMENT\n")
                git("add", "README.md"); new_tree = git("write-tree")
                if kind == "tree":
                    original, replacement = git("rev-parse", commit + "^{tree}"), new_tree
                else:
                    original = commit
                    replacement = git("-c", "user.name=Synthetic", "-c", "user.email=release@example.invalid",
                                      "commit-tree", new_tree, "-m", "Synthetic replacement")
            git("replace", original, replacement)
            self.assertNotEqual(git("cat-file", "-p", original, raw=True),
                                git("--no-replace-objects", "cat-file", "-p", original, raw=True))
            env = {"GIT_NO_REPLACE_OBJECTS": "0", "GIT_REPLACE_REF_BASE": "refs/replace/"} if interference else {}
            with patch.dict(os.environ, env):
                self.assertEqual(release.read_commit(root, commit), files)
            self.assertEqual(git("rev-parse", "HEAD"), commit)
            self.assertTrue(git("for-each-ref", "--format=%(refname)", "refs/replace"))

    def test_blob_replacement_does_not_change_export(self): self.check_replacement("blob")
    def test_commit_replacement_does_not_change_export(self): self.check_replacement("commit")
    def test_tree_replacement_does_not_change_export(self): self.check_replacement("tree")
    def test_manifest_replacement_does_not_change_export(self): self.check_replacement("manifest")
    def test_build_config_replacement_does_not_change_export(self): self.check_replacement("config")
    def test_environment_interference_cannot_restore_replacement(self): self.check_replacement("blob", True)
