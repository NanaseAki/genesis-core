"""Check committed whitespace using GitHub event SHAs; no fetch or fallback."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys


ZERO_SHA = "0" * 40


class DiffCheckError(RuntimeError):
    pass


def event_field(payload: dict, *keys: str):
    value = payload
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise DiffCheckError("missing event field: " + ".".join(keys))
        value = value[key]
    return value


def git_output(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if result.returncode:
        raise DiffCheckError(f"git {args[0]} failed; required history or baseline is unavailable")
    return result.stdout.strip()


def commit(repo: Path, value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", value) or value == ZERO_SHA:
        raise DiffCheckError("a nonzero full commit SHA is required")
    value = value.lower()
    if git_output(repo, "cat-file", "-t", value) != "commit":
        raise DiffCheckError("the requested SHA is not a commit")
    return value


def require_actual_root(repo: Path, head: str) -> None:
    """Check original commit headers, including across shallow boundaries."""
    actual = git_output(repo, "--no-replace-objects", "rev-parse", "--verify", "HEAD")
    if commit(repo, actual) != head:
        raise DiffCheckError("initial push after SHA does not match actual checkout HEAD")
    headers = git_output(repo, "--no-replace-objects", "cat-file", "commit", head).split("\n\n", 1)[0]
    if any(line.startswith("parent ") for line in headers.splitlines()):
        raise DiffCheckError("initial default-branch push requires a true parentless root commit")


def comparison(
    repo: Path, event_name: str, payload: dict, checkout_sha: str | None = None
) -> tuple[str | None, str]:
    """A None base denotes a verified initial root, never a self-comparison."""
    if event_name == "pull_request":
        base = commit(repo, event_field(payload, "pull_request", "base", "sha"))
        head = commit(repo, event_field(payload, "pull_request", "head", "sha"))
        return git_output(repo, "merge-base", base, head), head
    if event_name == "push":
        head = commit(repo, event_field(payload, "after"))
        if checkout_sha is not None and commit(repo, checkout_sha) != head:
            raise DiffCheckError("push checkout SHA does not match the event target")
        ref = event_field(payload, "ref")
        if not isinstance(ref, str) or not ref.startswith("refs/heads/"):
            raise DiffCheckError("only branch push events are supported")
        before = event_field(payload, "before")
        if before != ZERO_SHA:
            return commit(repo, before), head
        if event_field(payload, "created") is not True:
            raise DiffCheckError("zero before SHA requires an explicit new-branch event")
        default = event_field(payload, "repository", "default_branch")
        if not isinstance(default, str) or not default:
            raise DiffCheckError("new branch requires a default-branch baseline")
        git_output(repo, "check-ref-format", "refs/heads/" + default)
        if ref == "refs/heads/" + default:
            if checkout_sha is None:
                raise DiffCheckError("initial default-branch push requires an explicit checkout SHA")
            require_actual_root(repo, head)
            return None, head
        base = git_output(repo, "rev-parse", "--verify", "--end-of-options",
                          "refs/remotes/origin/" + default + "^{commit}")
        return git_output(repo, "merge-base", base, head), head
    if event_name == "workflow_dispatch":
        return commit(repo, event_field(payload, "inputs", "base_sha")), commit(repo, checkout_sha)
    raise DiffCheckError("unsupported event; no comparison baseline selected")


def check_committed_diff(repo: Path, base: str | None, head: str) -> subprocess.CompletedProcess:
    if base is None:
        head = commit(repo, head)
        require_actual_root(repo, head)
        return subprocess.run(
            ["git", "--no-replace-objects", "-C", str(repo), "diff-tree", "--root",
             "--check", "--no-ext-diff", "-r", head, "--"],
            capture_output=True, text=True,
        )
    return subprocess.run(
        ["git", "-C", str(repo), "diff", "--check", "--no-ext-diff", base, head, "--"],
        capture_output=True, text=True,
    )


def main() -> int:
    try:
        event_name = os.environ["GITHUB_EVENT_NAME"]
        payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8-sig"))
        base, head = comparison(Path.cwd(), event_name, payload, os.environ.get("GITHUB_SHA"))
        if base is None:
            print(f"Checking root commit {head} ({event_name})")
        else:
            print(f"Checking committed diff {base}..{head} ({event_name})")
        result = check_committed_diff(Path.cwd(), base, head)
    except (DiffCheckError, KeyError, OSError, ValueError) as error:
        print(f"CI diff check failed: {error}", file=sys.stderr)
        return 2
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
