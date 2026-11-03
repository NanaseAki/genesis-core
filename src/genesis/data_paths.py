"""Resolve data roots without inspecting personal files or creating anything."""

import os
from pathlib import Path
import re
import sys

from genesis._git_config import bare_flag


def _is_bare_repository(path: Path) -> bool:
    """Recognize conventional bare Git metadata without a Git executable.

    Require the combined layout, a valid-looking HEAD and core.bare=true; an
    ordinary directory named objects/refs or containing a file HEAD is not enough.
    Only read configuration after the metadata layout has matched.
    """
    if not ((path / "HEAD").is_file() and (path / "config").is_file()
            and (path / "objects").is_dir()
            and ((path / "refs").is_dir() or (path / "packed-refs").is_file())):
        return False
    head = (path / "HEAD").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"ref: refs/[^\s\x00]+|[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", head):
        return False
    try:
        with (path / "config").open(encoding="utf-8-sig", newline="") as stream:
            text = stream.read(1024 * 1024 + 1)
        if len(text) > 1024 * 1024:
            raise ValueError("Git-like config exceeds inspection limit")
        return bare_flag(text)
    except UnicodeError as error:
        raise ValueError("cannot verify Git-like data-root ancestor") from error


def data_root_path(explicit: str | Path | None = None) -> Path:
    """Return an absolute, resolved path outside any enclosing Git repository.

    The guard follows existing symlinks/junctions. It is an accidental-placement
    guard, not protection against a hostile process changing links afterwards.
    Calling this function alone never creates the default data directory.
    """
    configured = explicit if explicit is not None else os.environ.get("GENESIS_DATA_ROOT")
    if configured is None:
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA")
            if not base:
                raise ValueError("LOCALAPPDATA is unavailable; supply an absolute data root")
            configured = Path(base) / "Genesis"
        elif sys.platform == "darwin":
            configured = Path.home() / "Library" / "Application Support" / "Genesis"
        else:
            configured = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "Genesis"
    path = Path(configured)
    if not path.is_absolute():
        raise ValueError("data root must be absolute")
    # Check the explicit spelling as well as the resolved target: a link inside
    # a checkout that points outside must not bypass the accidental-placement
    # guard, and a link outside that points inside must also be rejected.
    lexical = Path(os.path.abspath(path))
    path = path.resolve()
    # Also reject the installed/source tree even when its .git is unavailable.
    source_tree = Path(__file__).resolve().parent
    checked: set[Path] = set()
    for candidate in (lexical, path):
        if candidate == source_tree or source_tree in candidate.parents:
            raise ValueError("data root cannot be in the source tree")
        for parent in (candidate, *candidate.parents):
            if parent in checked:
                continue
            checked.add(parent)
            marker = parent / ".git"
            if marker.exists() or marker.is_symlink() or _is_bare_repository(parent):
                raise ValueError("data root cannot be inside a Git repository")
    return path
