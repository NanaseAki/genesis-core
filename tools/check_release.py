"""Offline real-backend canaries. Uses only temporary synthetic Git repositories.

Run with an existing setuptools-capable interpreter; this script installs nothing.
"""
import argparse
import base64
import csv
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_prepare import (MANIFEST, ReleaseError, configuration, prepare,
                             read_commit, verify_artifacts, verify_snapshot,
                             verify_preparation, verify_roundtrip, check_backend, RECEIPT)


def metadata_canaries(repository, commit, checked, files, temp):
    """Tamper real backend outputs AND recompute the attacker's receipt."""
    rejected = []
    for case in ("console", "setup", "sources", "comment", "license", "license-metadata", "tar-directory"):
        output = temp / ("metadata-" + case); shutil.copytree(checked, output)
        dist = output / "dist"
        if case in ("console", "comment", "license", "license-metadata"):
            path = next(dist.glob("*.whl"))
            with zipfile.ZipFile(path) as archive:
                entries = {n: archive.read(n) for n in archive.namelist()}
            if case == "license":
                name = next(n for n in entries if n.endswith("/licenses/LICENSE"))
                entries[name] = b"SYNTHETIC_REPLACED_LICENSE"
            elif case == "license-metadata":
                name = next(n for n in entries if n.endswith("/METADATA"))
                entries[name] = entries[name].replace(b"License-Expression: Apache-2.0", b"License-Expression: MIT")
            if case == "console":
                name = next(n for n in entries if n.endswith("/entry_points.txt"))
                entries[name] = b"[console_scripts]\ngenesis-demo = genesis.day0:initial_state\n"
            if case != "comment":
                record = next(n for n in entries if n.endswith("/RECORD"))
                rows = io.StringIO(); writer = csv.writer(rows, lineterminator="\n")
                for name, data in entries.items():
                    writer.writerow([name, "", ""] if name == record else [name, "sha256=" +
                        base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode(), len(data)])
                entries[record] = rows.getvalue().encode()
            with zipfile.ZipFile(path, "w") as archive:
                for name, data in entries.items(): archive.writestr(name, data)
                if case == "comment": archive.comment = b"SYNTHETIC_HIDDEN_COMMENT"
        else:
            path = next(dist.glob("*.gz"))
            with tarfile.open(path) as archive:
                entries = {x.name: archive.extractfile(x).read() for x in archive if x.isfile()}
            if case == "tar-directory":
                directory = tarfile.TarInfo(path.name.removesuffix(".tar.gz"))
                directory.type = tarfile.DIRTYPE; directory.mode = 0o755; directory.size = 512
                # tarfile ignores directory size; the raw parser must reject before skipping.
                path.write_bytes(gzip.compress(directory.tobuf(format=tarfile.GNU_FORMAT)
                                               + gzip.decompress(path.read_bytes())))
            else:
                suffix = "/setup.cfg" if case == "setup" else "/SOURCES.txt"
                name = next(n for n in entries if n.endswith(suffix))
                entries[name] += (b"[options.data_files]\n. = ../synthetic.txt\n" if case == "setup"
                                  else b"synthetic_unapproved.py\n")
                with tarfile.open(path, "w:gz", format=tarfile.GNU_FORMAT) as archive:
                    for name, data in entries.items():
                        item = tarfile.TarInfo(name); item.size = len(data); archive.addfile(item, io.BytesIO(data))
        # Deliberately do not use the validator to create attacker-controlled hashes.
        artifacts = {}
        for path in dist.iterdir():
            if path.suffix == ".whl":
                with zipfile.ZipFile(path) as archive:
                    contents = {n: archive.read(n) for n in archive.namelist()}
            else:
                with tarfile.open(path) as archive:
                    contents = {x.name: archive.extractfile(x).read() for x in archive if x.isfile()}
            artifacts[path.name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "files": sorted(contents),
                "content_sha256": {n: hashlib.sha256(v).hexdigest() for n, v in sorted(contents.items())}}
        receipt = {"format_version": 1, "status": "private_preparation_not_authorized_release",
                   "files": {p: {"sha256": hashlib.sha256(v).hexdigest(), "bytes": len(v)} for p, v in files.items()},
                   "artifacts": artifacts}
        (output / RECEIPT).write_text(json.dumps(receipt), encoding="utf-8")
        try:
            verify_preparation(repository, commit, output)
        except ReleaseError:
            rejected.append(case)
        else:
            raise ReleaseError("metadata and rewritten receipt bypassed validation")
    return rejected


def run(repository: Path, commit: str, build_python: Path) -> dict:
    backend = check_backend(build_python)
    files = read_commit(repository, commit)
    configuration(files)
    with tempfile.TemporaryDirectory(prefix="genesis-release-canary-") as temporary:
        temp = Path(temporary)
        # This repository has synthetic history only; no original .git is copied.
        fixture = temp / "fixture"; fixture.mkdir()
        for name, data in files.items():
            path = fixture / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
        prefix = ["git", "-c", "safe.directory=" + str(fixture), "-c", "user.name=Synthetic Release Fixture",
                  "-c", "user.email=release@example.invalid", "-C", str(fixture)]
        for args in (["init", "--quiet"], ["add", "--", "."], ["commit", "--quiet", "-m", "Synthetic approved files"]):
            subprocess.run(prefix + args, check=True, capture_output=True, timeout=15)
        approved = subprocess.check_output(prefix + ["rev-parse", "HEAD"], timeout=15).decode().strip()
        canaries = {"src/genesis/private/secret.py": b"# SYNTHETIC_CANARY_ONLY\n",
                    "src/genesis/extra_module.py": b"# SYNTHETIC_CANARY_ONLY\n",
                    "src/genesis/.env": b"SYNTHETIC_ONLY=true\n",
                    "src/genesis/profile.json": b'{"synthetic":true}\n',
                    "src/genesis/state.sqlite3": b"SYNTHETIC_NOT_A_DATABASE\n",
                    "src/genesis/profile.txt": b"SYNTHETIC_CANARY_ONLY\n",
                    "private/account.py": b"# SYNTHETIC_CANARY_ONLY\n",
                    "OPEN_SOURCE_READINESS_AUDIT.md": b"# SYNTHETIC_CANARY_ONLY\n"}
        for name, data in canaries.items():
            path = fixture / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
        ignored = subprocess.check_output(prefix + ["check-ignore", "--no-index", "--", "src/genesis/private/secret.py"], timeout=15)
        if not ignored.strip():
            raise ReleaseError("ignored canary fixture failed")
        # Reproduce the reviewed old discovery rule, not a weakened assertion.
        old = temp / "old"; shutil.copytree(fixture, old, ignore=shutil.ignore_patterns(".git"))
        config = (old / "pyproject.toml").read_text(encoding="utf-8").split("[tool.setuptools]", 1)[0]
        (old / "pyproject.toml").write_text(config + '[tool.setuptools.packages.find]\nwhere = ["src"]\n', encoding="utf-8")
        (old / "MANIFEST.in").unlink()
        old_dist = temp / "old-dist"; old_dist.mkdir()
        command = "import setuptools.build_meta as b; b.build_sdist(" + repr(str(old_dist)) + "); b.build_wheel(" + repr(str(old_dist)) + ")"
        env = dict(os.environ, PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONDONTWRITEBYTECODE="1")
        for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
            env.pop(key, None)
        result = subprocess.run([str(build_python), "-B", "-I", "-c", command], cwd=old,
                                env=env, capture_output=True, timeout=120)
        if result.returncode:
            raise ReleaseError("old canary build did not run")
        old_results = {}
        for package in old_dist.iterdir():
            if package.suffix == ".whl":
                with zipfile.ZipFile(package) as archive:
                    names = archive.namelist()
            else:
                with tarfile.open(package, "r:gz") as archive:
                    names = archive.getnames()
            leaked = [p for p in names if p.endswith(("genesis/private/secret.py", "genesis/extra_module.py"))]
            if len(leaked) != 2:
                raise ReleaseError("old process must reproduce both private module leaks")
            old_results[package.name] = {"unapproved_module_count": len(leaked)}
        # Even committed extra files cannot enter the pinned explicit snapshot.
        subprocess.run(prefix + ["add", "-f", "--", *canaries], check=True, capture_output=True, timeout=15)
        subprocess.run(prefix + ["commit", "--quiet", "-m", "Synthetic extra files"], check=True, capture_output=True, timeout=15)
        newer = subprocess.check_output(prefix + ["rev-parse", "HEAD"], timeout=15).decode().strip()
        if read_commit(fixture, approved) != read_commit(fixture, newer):
            raise ReleaseError("unapproved commit files entered snapshot")
        receipt = prepare(fixture, newer, temp / "checked", build_python)
        verify_snapshot(temp / "checked/source", files)
        verify_artifacts(temp / "checked/dist", files)
        verify_preparation(fixture, newer, temp / "checked")
        roundtrip = verify_roundtrip(temp / "checked/dist", files, build_python)
        metadata_rejected = metadata_canaries(fixture, newer, temp / "checked", files, temp)
        if set(receipt["files"]) & set(canaries):
            raise ReleaseError("canary entered new source manifest")
        # Both source mutation and artifact injection are detected independently.
        extra = temp / "checked/source/src/genesis/extra_module.py"
        extra.write_bytes(b"# SYNTHETIC_CANARY_ONLY\n")
        try:
            verify_snapshot(temp / "checked/source", files)
        except ReleaseError:
            pass
        else:
            raise ReleaseError("post-export canary was accepted")
        extra.unlink()
        version, _ = configuration(files)
        wheel = temp / "checked/dist" / ("project_genesis-" + version + "-py3-none-any.whl")
        original_wheel = wheel.read_bytes()
        with zipfile.ZipFile(wheel, "a") as archive:
            archive.writestr("genesis/extra_module.py", b"# SYNTHETIC_CANARY_ONLY\n")
        try:
            verify_artifacts(temp / "checked/dist", files)
        except ReleaseError:
            pass
        else:
            raise ReleaseError("post-build canary was accepted")
        wheel.write_bytes(original_wheel)
        sdist = temp / "checked/dist" / ("project_genesis-" + version + ".tar.gz")
        original_sdist = sdist.read_bytes()
        with tarfile.open(sdist, "r:gz") as archive:
            members = [(item, None if item.isdir() else archive.extractfile(item).read()) for item in archive]
        with tarfile.open(sdist, "w:gz") as archive:
            for item, data in members:
                archive.addfile(item, None if data is None else io.BytesIO(data))
            extra_member = tarfile.TarInfo("project_genesis-" + version + "/src/genesis/extra_module.py")
            extra_member.size = 1; archive.addfile(extra_member, io.BytesIO(b"x"))
        try:
            verify_artifacts(temp / "checked/dist", files)
        except ReleaseError:
            pass
        else:
            raise ReleaseError("post-build sdist canary was accepted")
        sdist.write_bytes(original_sdist)
        return {"status": "private_preparation_checks_only", "old_process": old_results,
                "canaries_excluded": len(canaries), "source_file_count": len(files),
                "backend": backend, "roundtrip": roundtrip["runtime_and_metadata_equal"],
                "rewritten_receipt_metadata_rejected": metadata_rejected,
                "post_export_and_build_injection_rejected": True, "artifacts": receipt["artifacts"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--commit", required=True)
    parser.add_argument("--build-python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    print(json.dumps(run(args.repository, args.commit, args.build_python), ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
