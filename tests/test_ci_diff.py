"""Offline GitHub-event checks against a disposable, synthetic Git history."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "check_ci_diff.py"
spec = importlib.util.spec_from_file_location("genesis_ci_diff", SCRIPT)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class CiDiffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_parent = Path(tempfile.gettempdir()).resolve()
        cls.temporary = tempfile.TemporaryDirectory(prefix="genesis ci review ")
        cls.repo = Path(cls.temporary.name).resolve()
        cls.addClassCleanup(cls.cleanup_fixture)
        cls.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        cls.env.update({
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Genesis Synthetic Test",
            "GIT_COMMITTER_NAME": "Genesis Synthetic Test",
            "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
            "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
            "GIT_TERMINAL_PROMPT": "0",
            # Trust only this freshly created fixture, without editing Git config files.
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": str(cls.repo),
        })
        cls.git("init", "--initial-branch=baseline")
        cls.root = cls.make_commit("foundation.txt", "synthetic foundation\n", "synthetic root")
        cls.git("checkout", "-b", "topic")
        cls.early = cls.make_commit("early.txt", "synthetic early whitespace \n", "synthetic early")
        cls.head = cls.make_commit("later.txt", "synthetic later clean\n", "synthetic later")
        cls.clean = cls.make_commit("early.txt", "synthetic early fixed\n", "synthetic fixed")
        cls.git("checkout", "baseline")
        cls.base = cls.make_commit("base-only.txt", "synthetic base-only \n", "synthetic base advanced")
        cls.git("update-ref", "refs/remotes/origin/default-base", cls.base)
        tree = cls.git("rev-parse", cls.root + "^{tree}")
        cls.unrelated = cls.git("commit-tree", tree, input="synthetic unrelated root\n")
        cls.git("checkout", "--detach", cls.head)

    def setUp(self) -> None:
        environment = patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    @classmethod
    def cleanup_fixture(cls) -> None:
        if cls.repo.resolve().parent != cls.temp_parent:
            raise RuntimeError("refusing cleanup outside the synthetic fixture parent")
        cls.temporary.cleanup()

    @classmethod
    def git(cls, *args, input=None) -> str:
        return subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
             "-C", str(cls.repo), *args], input=input, env=cls.env,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    @classmethod
    def make_commit(cls, name: str, content: str, message: str) -> str:
        (cls.repo / name).write_text(content, encoding="utf-8", newline="\n")
        cls.git("add", "--", name)
        cls.git("commit", "-m", message)
        return cls.git("rev-parse", "HEAD")

    def push_event(self, *, before=None, after=None, created=False, default="default-base") -> dict:
        return {
            "before": self.root if before is None else before,
            "after": self.head if after is None else after,
            "created": created, "ref": "refs/heads/topic",
            "repository": {"default_branch": default},
        }

    def run_cli(self, event: str, payload: dict, checkout: str) -> subprocess.CompletedProcess:
        event_path = self.repo / "synthetic-event.json"
        event_path.write_text(json.dumps(payload), encoding="utf-8")
        env = dict(self.env, GITHUB_EVENT_NAME=event, GITHUB_EVENT_PATH=str(event_path),
                   GITHUB_SHA=checkout, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-B", "-I", "-S", str(SCRIPT)],
                              cwd=self.repo, env=env, capture_output=True, text=True)

    def test_push_checks_the_whole_committed_range_on_a_clean_checkout(self) -> None:
        self.assertEqual(self.git("diff", "--check"), "")
        base, head = ci.comparison(self.repo, "push", self.push_event(), self.head)
        self.assertEqual((base, head), (self.root, self.head))
        result = ci.check_committed_diff(self.repo, base, head)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("early.txt", result.stdout)
        self.assertEqual(ci.check_committed_diff(self.repo, self.early, self.head).returncode, 0)

    def test_new_branch_first_push_uses_default_branch_merge_base(self) -> None:
        event = self.push_event(before=ci.ZERO_SHA, created=True)
        base, head = ci.comparison(self.repo, "push", event, self.head)
        self.assertEqual((base, head), (self.root, self.head))
        self.assertNotEqual(ci.check_committed_diff(self.repo, base, head).returncode, 0)

    def test_pull_request_uses_event_head_and_common_ancestor(self) -> None:
        payload = {"pull_request": {"base": {"sha": self.base}, "head": {"sha": self.clean}}}
        base, head = ci.comparison(self.repo, "pull_request", payload, self.base)
        self.assertEqual((base, head), (self.root, self.clean))
        self.assertEqual(ci.check_committed_diff(self.repo, base, head).returncode, 0)

    def test_manual_run_requires_and_uses_an_explicit_base(self) -> None:
        selected = ci.comparison(self.repo, "workflow_dispatch", {"inputs": {"base_sha": self.root}},
                                 self.clean)
        self.assertEqual(selected, (self.root, self.clean))

    def test_missing_push_before_commit_fails_without_parent_fallback(self) -> None:
        with self.assertRaises(ci.DiffCheckError):
            ci.comparison(self.repo, "push", self.push_event(before="f" * 40), self.head)

    def test_missing_new_branch_baseline_or_initial_default_branch_fails(self) -> None:
        events = [self.push_event(before=ci.ZERO_SHA, created=True, default="missing-baseline"),
                  self.push_event(before=ci.ZERO_SHA, created=True)]
        events[1]["ref"] = "refs/heads/default-base"
        for event in events:
            with self.subTest(event=event), self.assertRaises(ci.DiffCheckError):
                ci.comparison(self.repo, "push", event, self.head)

    def test_invalid_event_fields_and_checkout_mismatch_fail(self) -> None:
        events = [self.push_event(before="--invalid"), self.push_event(after=ci.ZERO_SHA),
                  self.push_event(before=ci.ZERO_SHA), self.push_event()]
        events[3].pop("before")
        for event in events:
            with self.subTest(event=event), self.assertRaises(ci.DiffCheckError):
                ci.comparison(self.repo, "push", event, self.head)
        with self.assertRaises(ci.DiffCheckError):
            ci.comparison(self.repo, "push", self.push_event(), self.clean)

    def test_unrelated_pr_histories_fail_explicitly(self) -> None:
        payload = {"pull_request": {"base": {"sha": self.unrelated}, "head": {"sha": self.head}}}
        with self.assertRaises(ci.DiffCheckError):
            ci.comparison(self.repo, "pull_request", payload)

    def test_unsupported_event_or_missing_manual_base_fails(self) -> None:
        for event, payload in (("unknown", {}), ("workflow_dispatch", {})):
            with self.subTest(event=event), self.assertRaises(ci.DiffCheckError):
                ci.comparison(self.repo, event, payload, self.head)

    def test_cli_reports_committed_whitespace_and_returns_failure(self) -> None:
        result = self.run_cli("push", self.push_event(), self.head)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("early.txt", result.stdout)
        self.assertIn(self.root + ".." + self.head, result.stdout)

    def test_cli_missing_baseline_is_an_error_not_a_skipped_check(self) -> None:
        result = self.run_cli("push", self.push_event(before="f" * 40), self.head)
        self.assertEqual(result.returncode, 2)
        self.assertIn("baseline is unavailable", result.stderr)

    def test_cli_accepts_a_clean_explicit_manual_range(self) -> None:
        result = self.run_cli("workflow_dispatch", {"inputs": {"base_sha": self.root}}, self.clean)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(self.root + ".." + self.clean, result.stdout)


class InitialDefaultPushTests(unittest.TestCase):
    """A real disposable repository; never use the enclosing checkout's history."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="genesis initial push ")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name).resolve()
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update({
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Genesis Synthetic Test",
            "GIT_COMMITTER_NAME": "Genesis Synthetic Test",
            "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
            "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
            "GIT_TERMINAL_PROMPT": "0", "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": str(self.repo),
        })
        environment = patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.git("init", "--initial-branch=main")
        self.root = self.commit_files({"first.txt": "synthetic first\n",
                                       "nested/second.txt": "synthetic second\n"})

    def git(self, *args) -> str:
        return subprocess.run(
            ["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
             "-C", str(self.repo), *args], env=self.env, capture_output=True,
            text=True, check=True, timeout=15,
        ).stdout.strip()

    def commit_files(self, files: dict[str, str]) -> str:
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        self.git("add", "--", ".")
        self.git("commit", "-m", "Synthetic CI fixture")
        return self.git("rev-parse", "HEAD")

    def event(self, head: str | None = None) -> dict:
        return {"before": ci.ZERO_SHA, "after": head or self.root,
                "created": True, "ref": "refs/heads/main",
                "repository": {"default_branch": "main"}}

    def run_cli(self, payload: dict, checkout: str) -> subprocess.CompletedProcess:
        event = self.repo / "event.json"
        event.write_text(json.dumps(payload), encoding="utf-8")
        env = dict(self.env, GITHUB_EVENT_NAME="push", GITHUB_EVENT_PATH=str(event),
                   GITHUB_SHA=checkout)
        return subprocess.run([sys.executable, "-B", "-I", "-S", str(SCRIPT)],
                              cwd=self.repo, env=env, capture_output=True,
                              text=True, timeout=30)

    def test_initial_clean_root_cli_checks_real_root(self) -> None:
        self.assertNotIn("parent ", self.git("cat-file", "-p", self.root).split("\n\n", 1)[0])
        base, head = ci.comparison(self.repo, "push", self.event(), self.root)
        self.assertIsNone(base)
        self.assertEqual(head, self.root)
        result = self.run_cli(self.event(), self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("root commit " + self.root, result.stdout)

    def test_initial_dirty_root_checks_all_added_files_and_exit_code(self) -> None:
        self.git("checkout", "--orphan", "synthetic-dirty-root")
        dirty = self.commit_files({"first.txt": "synthetic first \n",
                                   "nested/second.txt": "synthetic second \n"})
        base, head = ci.comparison(self.repo, "push", self.event(dirty), dirty)
        self.assertIsNone(base)
        result = ci.check_committed_diff(self.repo, base, head)
        self.assertEqual(result.returncode, 2)
        self.assertIn("first.txt", result.stdout)
        self.assertIn("nested/second.txt", result.stdout)
        self.assertIn("trailing whitespace", result.stdout)
        cli = self.run_cli(self.event(dirty), dirty)
        self.assertEqual(cli.returncode, result.returncode)
        self.assertIn("nested/second.txt", cli.stdout)
        self.assertNotIn("CI diff check failed", cli.stderr)

    def test_nonroot_cannot_claim_zero_before(self) -> None:
        head = self.commit_files({"later.txt": "synthetic later\n"})
        with self.assertRaises(ci.DiffCheckError):
            ci.comparison(self.repo, "push", self.event(head), head)
        self.assertEqual(self.run_cli(self.event(head), head).returncode, 2)

    def test_after_must_match_actual_head_even_if_checkout_argument_matches(self) -> None:
        self.commit_files({"later.txt": "synthetic later\n"})
        with self.assertRaises(ci.DiffCheckError):
            ci.comparison(self.repo, "push", self.event(), self.root)
        self.assertEqual(self.run_cli(self.event(), self.root).returncode, 2)

    def test_checkout_argument_must_match_event_after(self) -> None:
        other = self.commit_files({"later.txt": "synthetic later\n"})
        self.git("checkout", "--detach", self.root)
        with self.assertRaises(ci.DiffCheckError):
            ci.comparison(self.repo, "push", self.event(), other)

    def test_initial_root_requires_explicit_checkout_sha(self) -> None:
        with self.assertRaises(ci.DiffCheckError):
            ci.comparison(self.repo, "push", self.event())

    def test_wrong_branch_cannot_use_initial_default_root_path(self) -> None:
        event = self.event()
        event["ref"] = "refs/heads/topic"
        with self.assertRaises(ci.DiffCheckError):
            ci.comparison(self.repo, "push", event, self.root)
        self.assertEqual(self.run_cli(event, self.root).returncode, 2)

    def test_initial_root_requires_boolean_created_and_zero_before(self) -> None:
        for value in (False, 1, "true", None):
            with self.subTest(created=value):
                event = self.event()
                event["created"] = value
                with self.assertRaises(ci.DiffCheckError):
                    ci.comparison(self.repo, "push", event, self.root)
        event = self.event()
        event["before"] = "f" * 40
        with self.assertRaises(ci.DiffCheckError):
            ci.comparison(self.repo, "push", event, self.root)

    def test_shallow_boundary_is_not_a_true_root(self) -> None:
        head = self.commit_files({"later.txt": "synthetic later\n"})
        (self.repo / ".git" / "shallow").write_text(head + "\n", encoding="ascii")
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", head), head)
        self.assertIn("parent ", self.git("cat-file", "-p", head).split("\n\n", 1)[0])
        with self.assertRaises(ci.DiffCheckError):
            ci.comparison(self.repo, "push", self.event(head), head)

    def test_root_execution_revalidates_parentlessness(self) -> None:
        head = self.commit_files({"later.txt": "synthetic later\n"})
        with self.assertRaises(ci.DiffCheckError):
            ci.check_committed_diff(self.repo, None, head)

    def test_pull_request_never_selects_root_mode(self) -> None:
        event = dict(self.event(), pull_request={"base": {"sha": self.root},
                                               "head": {"sha": self.root}})
        self.assertEqual(ci.comparison(self.repo, "pull_request", event, self.root),
                         (self.root, self.root))

    def test_ordinary_push_keeps_explicit_before_range(self) -> None:
        head = self.commit_files({"later.txt": "synthetic later \n"})
        event = self.event(head)
        event.update(before=self.root, created=False)
        base, selected = ci.comparison(self.repo, "push", event, head)
        self.assertEqual((base, selected), (self.root, head))
        result = ci.check_committed_diff(self.repo, base, selected)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("later.txt", result.stdout)
