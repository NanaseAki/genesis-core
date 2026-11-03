"""Private preparation only: pinned Git blobs -> allowlisted snapshot -> checked packages.

The manifest approves paths, not copyright or content safety. Review its changes
and the pinned commit. Never invoke a release build on an arbitrary work tree.
"""
import argparse
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
import zlib
sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_rules import BACKEND_VERSION, ReleaseError, approved_licenses, verify_container, verify_metadata

MANIFEST = "release_files.json"
RECEIPT = "SNAPSHOT_CHECKSUMS.json"
LIMIT = 1024 * 1024
TOTAL_LIMIT = 16 * LIMIT
MAX_FILES = 512
ROOT_FILES = {"README.md", "AGENTS.md", "CONTRIBUTING.md", "SECURITY.md",
              "CODE_OF_CONDUCT.md", "pyproject.toml", "MANIFEST.in", MANIFEST,
              ".gitignore", ".gitattributes", "LICENSE", "NOTICE"}
FORBIDDEN = re.compile(r"private|secret|credential|audit|delivery|chat|backup|memory_data", re.I)
RESERVED = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", re.I)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_path(name: str) -> str:
    if (not isinstance(name, str) or not name or len(name) > 240
            or not re.fullmatch(r"[A-Za-z0-9_.\-/]+", name)
            or name.startswith("/") or "//" in name):
        raise ReleaseError("invalid relative file path")
    if any(p in {"", ".", ".."} or p.endswith((".", " ")) or RESERVED.fullmatch(p)
           for p in name.split("/")):
        raise ReleaseError("ambiguous relative file path")
    return name


def approved_path(name: str) -> str:
    safe_path(name)
    if name in ROOT_FILES:
        return name
    parts = PurePosixPath(name).parts
    if any(p.startswith(".") for p in (parts[1:] if parts[0] == ".github" else parts)):
        raise ReleaseError("unapproved hidden path")
    if FORBIDDEN.search(name):
        raise ReleaseError("private or operational path forbidden")
    if ((name.startswith("src/genesis/") or name.startswith(("tests/", "tools/")))
            and name.endswith(".py")):
        return name
    if name.startswith("docs/") and name.endswith(".md") and not re.search(r"(?:review|final)_fixes", name, re.I):
        return name
    if name == ".github/workflows/ci.yml" or name == ".github/pull_request_template.md":
        return name
    if name.startswith(".github/ISSUE_TEMPLATE/") and name.endswith(".yml"):
        return name
    raise ReleaseError("file type or location not approved")


def manifest_text(paths: list[str]) -> bytes:
    return ("# Generated from the explicit release_files.json list; no glob rules.\n"
            + "".join("include " + p + "\n" for p in paths)).encode("ascii")


def _json(data: bytes) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ReleaseError("duplicate JSON key")
            value[key] = item
        return value
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeError) as error:
        raise ReleaseError("invalid preparation JSON") from None
    if not isinstance(value, dict):
        raise ReleaseError("preparation JSON must be an object")
    return value


def parse_manifest(data: bytes) -> list[str]:
    value = _json(data)
    if set(value) != {"format_version", "files"} or type(value["format_version"]) is not int or value["format_version"] != 1:
        raise ReleaseError("unsupported source manifest")
    paths = value["files"]
    if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_FILES:
        raise ReleaseError("invalid source file count")
    for path in paths:
        approved_path(path)
    if len({p.casefold() for p in paths}) != len(paths) or paths != sorted(paths):
        raise ReleaseError("duplicate, case-colliding or unordered source paths")
    if not {MANIFEST, "MANIFEST.in", "README.md", "pyproject.toml"} <= set(paths):
        raise ReleaseError("missing source control files")
    return paths


def _git(repository: Path, *args: str) -> bytes:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    for key in list(env):
        if key.upper().startswith("GIT_") and key not in {"GIT_OPTIONAL_LOCKS", "GIT_TERMINAL_PROMPT"}:
            del env[key]
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    result = subprocess.run(["git", "--no-replace-objects", "-c", "safe.directory=" + str(repository), "-C", str(repository), *args],
                            capture_output=True, env=env, timeout=30)
    if result.returncode:
        raise ReleaseError("Git source inspection failed")
    return result.stdout


def read_commit(repository: Path, commit: str) -> dict[str, bytes]:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReleaseError("a full immutable commit SHA is required")
    if _git(repository, "cat-file", "-t", commit).strip() != b"commit":
        raise ReleaseError("source is not a commit")
    entries = {}
    for row in _git(repository, "ls-tree", "-rlz", "--full-tree", commit).split(b"\0"):
        if not row:
            continue
        header, path = row.split(b"\t", 1)
        mode, kind, oid, size = header.split()
        entries[path.decode("utf-8")] = (mode, kind, oid, size)
    def read(path):
        item = entries.get(path)
        if item is None or item[0] not in (b"100644", b"100755") or item[1] != b"blob":
            raise ReleaseError("missing or nonregular approved Git file")
        if int(item[3]) > LIMIT:
            raise ReleaseError("approved source file exceeds limit")
        data = _git(repository, "cat-file", "blob", item[2].decode("ascii"))
        if hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest().encode("ascii") != item[2]:
            raise ReleaseError("source blob differs from declared object ID")
        try:
            data.decode("utf-8")
        except UnicodeError:
            raise ReleaseError("source is not UTF-8 text") from None
        if b"\0" in data:
            raise ReleaseError("binary source is forbidden")
        return data
    paths = parse_manifest(read(MANIFEST))
    files = {p: read(p) for p in paths}
    if sum(map(len, files.values())) > TOTAL_LIMIT:
        raise ReleaseError("source snapshot exceeds limit")
    if files["MANIFEST.in"] != manifest_text(paths):
        raise ReleaseError("sdist manifest differs from explicit source list")
    return files


def configuration(files: dict[str, bytes]) -> tuple[str, list[str]]:
    config = tomllib.loads(files["pyproject.toml"].decode("utf-8"))
    build, project, setup = config.get("build-system", {}), config.get("project", {}), config.get("tool", {}).get("setuptools", {})
    if (set(config) != {"build-system", "project", "tool"}
            or set(config["tool"]) != {"setuptools"}
            or set(project) - {"name", "version", "description", "readme", "requires-python", "dependencies", "scripts", "license", "license-files"}
            or project.get("readme") != "README.md" or project.get("dependencies", []) != []):
        raise ReleaseError("unreviewed metadata or external build inputs")
    if build != {"requires": ["setuptools==" + BACKEND_VERSION], "build-backend": "setuptools.build_meta"}:
        raise ReleaseError("unreviewed build backend configuration")
    for key in ("name", "version", "description", "requires-python"):
        if key in project and (not isinstance(project[key], str) or not project[key]
                               or any(c in project[key] for c in "\r\n\0")):
            raise ReleaseError("invalid approved metadata field")
    scripts = project.get("scripts", {})
    if (not isinstance(scripts, dict) or any(
            not re.fullmatch(r"[a-z][a-z0-9-]*", name) or not isinstance(target, str)
            or not re.fullmatch(r"genesis(?:\.[a-z_][a-z0-9_]*)+:[a-z_][a-z0-9_]*", target)
            for name, target in scripts.items())):
        raise ReleaseError("unreviewed console configuration")
    approved_licenses(files)
    version = project.get("version", "")
    if project.get("name") != "project-genesis" or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ReleaseError("unreviewed distribution identity")
    packages = sorted(p[len("src/"):-len("/__init__.py")].replace("/", ".")
                      for p in files if p.startswith("src/genesis/") and p.endswith("/__init__.py"))
    if (set(setup) != {"package-dir", "packages", "include-package-data"}
            or setup["package-dir"] != {"": "src"} or setup["include-package-data"] is not False
            or sorted(setup["packages"]) != packages):
        raise ReleaseError("packages must be explicit and match the source list")
    return version, packages


def _regular_files(root: Path) -> tuple[set[str], set[str]]:
    if root.is_symlink() or root.is_junction() or not root.is_dir():
        raise ReleaseError("snapshot root must be a regular directory")
    files, folders = set(), set()
    for current, directories, names in os.walk(root, followlinks=False):
        for name in directories + names:
            path = Path(current) / name
            if path.is_symlink() or path.is_junction():
                raise ReleaseError("snapshot links are forbidden")
            mode = path.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ReleaseError("snapshot special files are forbidden")
            if stat.S_ISREG(mode):
                files.add(path.relative_to(root).as_posix())
            else:
                folders.add(path.relative_to(root).as_posix())
    return files, folders


def verify_snapshot(root: Path, files: dict[str, bytes]) -> None:
    expected_folders = {p.as_posix() for name in files for p in PurePosixPath(name).parents if p != PurePosixPath(".")}
    if _regular_files(root) != (set(files), expected_folders):
        raise ReleaseError("snapshot file set changed")
    for name, expected in files.items():
        path = root / name
        if path.stat().st_size > LIMIT or path.read_bytes() != expected:
            raise ReleaseError("snapshot content changed")


def _archive_entries(path: Path) -> dict[str, bytes]:
    try:
        return _checked_archive_entries(path)
    except (UnicodeError, zipfile.BadZipFile, tarfile.TarError, zlib.error, EOFError, OSError):
        raise ReleaseError("invalid archive contents") from None


def _checked_archive_entries(path: Path) -> dict[str, bytes]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > TOTAL_LIMIT * 2:
        raise ReleaseError("invalid archive file")
    try:
        verify_container(path)
    except (UnicodeError, zipfile.BadZipFile, tarfile.TarError, EOFError, OSError, ValueError) as error:
        if isinstance(error, ReleaseError):
            raise
        raise ReleaseError("invalid archive container") from None
    entries = {}; names = set(); total = 0; folders = set()
    def admit(name, size):
        nonlocal total
        safe_path(name)
        if name.casefold() in names or size > LIMIT or size < 0:
            raise ReleaseError("ambiguous or oversized archive entry")
        names.add(name.casefold()); total += size
        if len(names) > MAX_FILES * 4 or total > TOTAL_LIMIT * 2:
            raise ReleaseError("archive budget exceeded")
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                name = item.orig_filename[:-1] if item.is_dir() else item.orig_filename
                admit(name, item.file_size)
                mode = item.external_attr >> 16
                if item.orig_filename != item.filename or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ReleaseError("archive link or abnormal filename")
                if item.is_dir():
                    if stat.S_IFMT(mode) not in (0, stat.S_IFDIR):
                        raise ReleaseError("archive directory mode mismatch")
                    folders.add(name)
                    continue
                if stat.S_IFMT(mode) == stat.S_IFDIR:
                    raise ReleaseError("archive file mode mismatch")
                entries[name] = archive.read(item)
    else:
        with tarfile.open(path, "r:gz") as archive:
            for item in archive:
                admit(item.name, item.size)
                if (set(item.pax_headers) - {"mtime"}
                        or any(not re.fullmatch(r"[0-9]{1,20}(?:\.[0-9]{1,9})?", v) for v in item.pax_headers.values())):
                    raise ReleaseError("archive metadata overrides forbidden")
                if item.isdir():
                    if item.size != 0:
                        raise ReleaseError("tar directory size must be zero")
                    folders.add(item.name)
                    continue
                if not item.isfile():
                    raise ReleaseError("archive links, special files or overrides forbidden")
                entries[item.name] = archive.extractfile(item).read()
    parents = {p.as_posix() for name in entries for p in PurePosixPath(name).parents if p != PurePosixPath(".")}
    if not folders <= parents:
        raise ReleaseError("unapproved empty archive directory")
    return entries


def verify_artifacts(dist: Path, files: dict[str, bytes]) -> dict:
    version, packages = configuration(files)
    stem = "project_genesis-" + version
    wheel, sdist = dist / (stem + "-py3-none-any.whl"), dist / (stem + ".tar.gz")
    if {p.name for p in dist.iterdir()} != {wheel.name, sdist.name}:
        raise ReleaseError("unexpected distribution outputs")
    w, s = _archive_entries(wheel), _archive_entries(sdist)
    runtime = {p[4:]: data for p, data in files.items() if p.startswith("src/genesis/")}
    info = stem + ".dist-info/"
    wheel_meta = {info + n for n in ("METADATA", "WHEEL", "entry_points.txt", "top_level.txt", "RECORD")}
    license_members = {info + "licenses/" + name: files[name] for name in approved_licenses(files)}
    if set(w) != set(runtime) | wheel_meta | set(license_members):
        raise ReleaseError("wheel has missing or unapproved files")
    source_meta = {"PKG-INFO", "setup.cfg"} | {"src/project_genesis.egg-info/" + n for n in
                    ("PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt", "top_level.txt")}
    if set(s) != {stem + "/" + p for p in set(files) | source_meta}:
        raise ReleaseError("sdist has missing or unapproved files")
    for p, data in files.items():
        if s[stem + "/" + p] != data:
            raise ReleaseError("sdist source bytes changed")
    for p, data in (runtime | license_members).items():
        if w[p] != data:
            raise ReleaseError("wheel source bytes changed")
    verify_metadata(files, w, s, version)
    rows = list(csv.reader(io.StringIO(w[info + "RECORD"].decode("utf-8"))))
    if any(len(row) != 3 for row in rows) or len(rows) != len(w) or {row[0] for row in rows} != set(w):
        raise ReleaseError("invalid wheel RECORD inventory")
    for p, sha, size in rows:
        if p == info + "RECORD":
            if sha or size:
                raise ReleaseError("invalid wheel self record")
        elif sha != "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(w[p]).digest()).rstrip(b"=").decode() or size != str(len(w[p])):
            raise ReleaseError("wheel RECORD checksum mismatch")
    return {p.name: {"sha256": digest(p.read_bytes()), "files": sorted(entries),
                    "content_sha256": {n: digest(v) for n, v in sorted(entries.items())}}
            for p, entries in ((wheel, w), (sdist, s))}


def _output_location(repository: Path, output: Path, *, existing: bool) -> None:
    if not output.is_absolute() or output.exists() != existing:
        raise ReleaseError("output existence or absolute path requirement failed")
    for parent in (output, *output.parents):
        git_layout = ((parent / "HEAD").is_file() and (parent / "config").is_file()
                      and (parent / "objects").is_dir()
                      and ((parent / "refs").is_dir() or (parent / "packed-refs").is_file()))
        if parent.is_symlink() or parent.is_junction() or (parent / ".git").exists() or git_layout:
            raise ReleaseError("output must be outside repositories and directory links")
    if output.resolve().is_relative_to(repository.resolve()):
        raise ReleaseError("output cannot be inside source repository")


def _receipt(files: dict[str, bytes], artifacts: dict) -> dict:
    return {"format_version": 1, "status": "private_preparation_not_authorized_release",
            "files": {p: {"sha256": digest(v), "bytes": len(v)} for p, v in files.items()},
            "artifacts": artifacts}


def verify_preparation(repository: Path, commit: str, output: Path) -> dict:
    """Recheck against pinned Git blobs, not against untrusted hashes alone."""
    _output_location(repository, output, existing=True)
    files = read_commit(repository, commit)
    verify_snapshot(output / "source", files)
    artifacts = verify_artifacts(output / "dist", files)
    paths = {"source/" + p for p in files} | {"dist/" + p for p in artifacts} | {RECEIPT}
    folders = {p.as_posix() for name in paths for p in PurePosixPath(name).parents if p != PurePosixPath(".")}
    if _regular_files(output) != (paths, folders):
        raise ReleaseError("unexpected preparation output files")
    receipt_path = output / RECEIPT
    if receipt_path.stat().st_size > LIMIT:
        raise ReleaseError("receipt exceeds limit")
    receipt = _receipt(files, artifacts)
    if _json(receipt_path.read_bytes()) != receipt:
        raise ReleaseError("receipt differs from source and actual artifacts")
    return receipt


def prepare(repository: Path, commit: str, output: Path, build_python: Path) -> dict:
    repository, output = Path(repository).absolute(), Path(output)
    if not build_python.is_absolute() or not build_python.is_file():
        raise ReleaseError("an existing absolute trusted build interpreter is required")
    _output_location(repository, output, existing=False)
    files = read_commit(repository, commit)
    configuration(files)
    check_backend(build_python)
    with tempfile.TemporaryDirectory(prefix="genesis-private-release-") as temporary:
        temp = Path(temporary); finished = temp / "checked"; source = finished / "source"
        source.mkdir(parents=True)
        for name, data in files.items():
            target = source / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
        verify_snapshot(source, files)
        build = temp / "build"; shutil.copytree(source, build)
        verify_snapshot(build, files)
        dist = finished / "dist"; dist.mkdir()
        env = dict(os.environ, PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONDONTWRITEBYTECODE="1")
        for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
            env.pop(key, None)
        command = "import setuptools.build_meta as b; b.build_sdist(" + repr(str(dist)) + "); b.build_wheel(" + repr(str(dist)) + ")"
        result = subprocess.run([str(build_python), "-B", "-I", "-c", command], cwd=build,
                                env=env, capture_output=True, timeout=120)
        if result.returncode:
            raise ReleaseError("offline preparation build failed; existing setuptools backend required")
        artifacts = verify_artifacts(dist, files)
        verify_snapshot(source, files)
        receipt = _receipt(files, artifacts)
        (finished / RECEIPT).write_text(json.dumps(receipt, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        shutil.copytree(finished, output)
        if verify_preparation(repository, commit, output) != receipt:
            raise ReleaseError("output artifacts changed")
        return receipt


def check_backend(build_python: Path) -> dict:
    """Inspect an existing trusted interpreter; never install a backend here."""
    command = "import json,sys,setuptools; print(json.dumps({'python':sys.version.split()[0], 'setuptools':setuptools.__version__}))"
    result = subprocess.run([str(build_python), "-B", "-I", "-c", command],
                            capture_output=True, timeout=30)
    if result.returncode:
        raise ReleaseError("existing trusted setuptools backend required")
    value = _json(result.stdout)
    if value.get("setuptools") != BACKEND_VERSION:
        raise ReleaseError("unsupported build backend version")
    return value


def verify_roundtrip(dist: Path, files: dict[str, bytes], build_python: Path) -> dict:
    """Validate candidate bytes/configuration BEFORE executing any sdist build."""
    original = verify_artifacts(dist, files)
    backend = check_backend(build_python)
    version, _ = configuration(files)
    stem = "project_genesis-" + version
    sdist = dist / (stem + ".tar.gz")
    members = _archive_entries(sdist)
    with tempfile.TemporaryDirectory(prefix="genesis-verified-roundtrip-") as temporary:
        root = Path(temporary); source = root / "source"; source.mkdir()
        # Members have passed exact inventory, path, type and metadata validation.
        for name, data in members.items():
            target = source / name[len(stem) + 1:]
            target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
        rebuilt = root / "dist"; rebuilt.mkdir()
        env = dict(os.environ, PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONDONTWRITEBYTECODE="1")
        for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
            env.pop(key, None)
        command = "import setuptools.build_meta as b; b.build_wheel(" + repr(str(rebuilt)) + ")"
        result = subprocess.run([str(build_python), "-B", "-I", "-c", command], cwd=source,
                                env=env, capture_output=True, timeout=120)
        if result.returncode:
            raise ReleaseError("verified sdist roundtrip build failed")
        shutil.copyfile(sdist, rebuilt / sdist.name)
        checked = verify_artifacts(rebuilt, files)
        wheel = stem + "-py3-none-any.whl"
        if checked[wheel]["content_sha256"] != original[wheel]["content_sha256"]:
            raise ReleaseError("roundtrip wheel content differs")
        return {"backend": backend, "runtime_and_metadata_equal": True,
                "artifacts": checked}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--build-python", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--roundtrip", action="store_true")
    parser.add_argument("--private-preparation", action="store_true", required=True)
    args = parser.parse_args()
    if args.verify_only:
        receipt = verify_preparation(args.repository, args.commit, args.output)
    else:
        if args.build_python is None:
            parser.error("preparation requires --build-python")
        receipt = prepare(args.repository, args.commit, args.output, args.build_python)
    if args.roundtrip:
        if args.build_python is None:
            parser.error("--roundtrip requires --build-python")
        verify_roundtrip(args.output / "dist", read_commit(args.repository, args.commit), args.build_python)
    print(json.dumps(receipt, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
