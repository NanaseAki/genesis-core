"""Synthetic source and archive attacks; no build backend or private data needed."""
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import release_prepare as release


def source_files() -> dict[str, bytes]:
    files = {"LICENSE": (Path(__file__).resolve().parents[1] / "LICENSE").read_bytes(), "README.md": b"Synthetic preparation fixture\n", ".gitignore": b"private/\n*.sqlite3\n.env\n",
             "pyproject.toml": b'''[build-system]
requires = ["setuptools==84.1.0"]
build-backend = "setuptools.build_meta"
[project]
name = "project-genesis"
version = "0.2.0"
readme = "README.md"
license = "Apache-2.0"
license-files = ["LICENSE"]
[tool.setuptools]
package-dir = {"" = "src"}
packages = ["genesis"]
include-package-data = false
''', "src/genesis/__init__.py": b"# Synthetic module\n",
             "tests/helper.py": b"# Synthetic helper\n", "tools/check.py": b"# Synthetic tool\n"}
    paths = sorted([*files, release.MANIFEST, "MANIFEST.in"])
    files[release.MANIFEST] = json.dumps({"format_version": 1, "files": paths}).encode()
    files["MANIFEST.in"] = release.manifest_text(paths)
    return files


def write_files(root: Path, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        p = root / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)


def synthetic_git(root: Path, files: dict[str, bytes]) -> str:
    write_files(root, files)
    prefix = ["git", "-c", "safe.directory=" + str(root), "-c", "user.name=Synthetic Release Test",
              "-c", "user.email=release@example.invalid", "-C", str(root)]
    for args in (["init", "--quiet"], ["add", "--", "."], ["commit", "--quiet", "-m", "Synthetic approved source"]):
        subprocess.run(prefix + args, check=True, capture_output=True, timeout=10)
    return subprocess.check_output(prefix + ["rev-parse", "HEAD"], timeout=10).decode().strip()


def archive_pair(root: Path, files: dict[str, bytes], *, extra_wheel=None, extra_sdist=None,
                 change_wheel=None, change_sdist=None) -> None:
    stem = "project_genesis-0.2.0"; info = stem + ".dist-info/"
    project = tomllib.loads(files["pyproject.toml"].decode("utf-8"))["project"]
    headers = ["Metadata-Version: 2.4", "Name: project-genesis", "Version: 0.2.0"]
    for key, field in (("description", "Summary"), ("requires-python", "Requires-Python")):
        if key in project: headers.append(field + ": " + project[key])
    headers += ["License-Expression: " + project["license"], "License-File: LICENSE", "Dynamic: license-file"]
    metadata = ("\n".join(headers) + "\nDescription-Content-Type: text/markdown\n\n").encode() + files["README.md"]
    scripts = project.get("scripts", {})
    points = ("[console_scripts]\n" + "".join(k + " = " + v + "\n" for k, v in sorted(scripts.items()))).encode() if scripts else b""
    wheel = {p[4:]: data for p, data in files.items() if p.startswith("src/genesis/")}
    wheel.update({info + name: data for name, data in {
        "METADATA": metadata,
        "WHEEL": b"Wheel-Version: 1.0\nGenerator: setuptools (84.1.0)\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
        "entry_points.txt": points, "top_level.txt": b"genesis\n"}.items()})
    wheel[info + "licenses/LICENSE"] = files["LICENSE"]
    wheel.update(extra_wheel or {})
    wheel.update(change_wheel or {})
    record = io.StringIO(); writer = csv.writer(record, lineterminator="\n")
    for name, data in wheel.items():
        writer.writerow([name, "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode(), len(data)])
    writer.writerow([info + "RECORD", "", ""]); wheel[info + "RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(root / (stem + "-py3-none-any.whl"), "w") as z:
        for name, data in wheel.items(): z.writestr(name, data)
    source = dict(files)
    source.update({"PKG-INFO": wheel[info + "METADATA"], "setup.cfg": b"[egg_info]\ntag_build = \ntag_date = 0\n\n"})
    generated = {"PKG-INFO": wheel[info + "METADATA"], "SOURCES.txt": b"",
                 "dependency_links.txt": b"\n", "entry_points.txt": points, "top_level.txt": b"genesis\n"}
    generated["SOURCES.txt"] = ("\n".join(sorted(set(files) | {"src/project_genesis.egg-info/" + p for p in generated})) + "\n").encode()
    source.update({"src/project_genesis.egg-info/" + name: data for name, data in generated.items()})
    source.update(extra_sdist or {}); source.update(change_sdist or {})
    with tarfile.open(root / (stem + ".tar.gz"), "w:gz", format=tarfile.GNU_FORMAT) as t:
        for name, data in source.items():
            member = tarfile.TarInfo(stem + "/" + name); member.size = len(data)
            t.addfile(member, io.BytesIO(data))


class ReleasePathTests(unittest.TestCase):
    def test_check_release_entry_works_with_isolated_no_site_python(self):
        tool = Path(__file__).resolve().parents[1] / "tools/check_release.py"
        result = subprocess.run([sys.executable, "-B", "-I", "-S", str(tool), "--help"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--commit", result.stdout)

    def test_approved_public_paths_include_helpers_and_tools(self):
        for path in ("src/genesis/models/ollama.py", "tests/http_synthetic_raw.py", "tests/test_review_fixes.py",
                     "tools/release_prepare.py", "docs/ARCHITECTURE.md", ".github/workflows/ci.yml"):
            self.assertEqual(release.approved_path(path), path)

    def test_traversal_absolute_windows_and_unicode_paths_fail_closed(self):
        for path in ("../module.py", "/module.py", "C:/module.py", "src\\genesis\\x.py", "src//x.py",
                     "src/./x.py", "src/genesis/../x.py", "src/genesis/x.py:stream", "src/genesis/中.py", "src/genesis/x\0.py"):
            with self.subTest(path=ascii(path)), self.assertRaises(release.ReleaseError): release.safe_path(path)

    def test_reserved_names_and_trailing_dot_are_rejected(self):
        for name in ("src/genesis/CON.py", "src/genesis/com1.py", "src/genesis/LPT9.py", "src/genesis/x./y.py"):
            with self.assertRaises(release.ReleaseError): release.safe_path(name)

    def test_private_types_and_operational_documents_are_forbidden(self):
        for name in ("src/genesis/private/x.py", "src/genesis/secret_config.py", "src/genesis/state.sqlite3",
                     "src/genesis/.env", "src/genesis/profile.json", "src/genesis/profile.txt",
                     "OPEN_SOURCE_READINESS_AUDIT.md", "docs/M2B1_DELIVERY.md", "docs/M2A_REVIEW_FIXES.md", "docs/M2B1_FINAL_FIXES.md",
                     ".git/config", ".github/ISSUE_TEMPLATE/.env.yml", ".github/ISSUE_TEMPLATE/.git/config.yml",
                     "model.gguf", "outside.py"):
            with self.subTest(name=name), self.assertRaises(release.ReleaseError): release.approved_path(name)

    def test_manifest_rejects_duplicate_case_and_unknown_schema(self):
        paths = json.loads(source_files()[release.MANIFEST])["files"]
        for value in ({"format_version": True, "files": paths}, {"format_version": 2, "files": paths},
                      {"format_version": 1, "files": paths + [paths[0]]},
                      {"format_version": 1, "files": sorted(paths + ["readme.md"])},
                      {"format_version": 1, "files": list(reversed(paths))}):
            with self.assertRaises(release.ReleaseError): release.parse_manifest(json.dumps(value).encode())
        with self.assertRaises(release.ReleaseError): release.parse_manifest(b'{"files":[],"files":[],"format_version":1}')


class ReleaseSourceTests(unittest.TestCase):
    def test_pinned_git_blobs_exclude_tracked_and_untracked_canaries(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); files = source_files(); commit = synthetic_git(root, files)
            canaries = {"src/genesis/private/secret.py": b"# SYNTHETIC_CANARY\n",
                        "src/genesis/extra_module.py": b"# SYNTHETIC_CANARY\n", ".env": b"SYNTHETIC=true\n",
                        "src/genesis/profile.json": b"{}", "runtime/state.sqlite3": b"SYNTHETIC_DB"}
            write_files(root, canaries)
            self.assertEqual(release.read_commit(root, commit), {p: files[p] for p in sorted(files)})
            prefix = ["git", "-c", "safe.directory=" + str(root), "-C", str(root)]
            subprocess.run(prefix + ["add", "-f", "--", *canaries], check=True, capture_output=True, timeout=10)
            subprocess.run(prefix + ["-c", "user.name=Synthetic", "-c", "user.email=release@example.invalid",
                                     "commit", "--quiet", "-m", "Synthetic unapproved files"], check=True, capture_output=True, timeout=10)
            newer = subprocess.check_output(prefix + ["rev-parse", "HEAD"], timeout=10).decode().strip()
            self.assertEqual(release.read_commit(root, newer), {p: files[p] for p in sorted(files)})

    def test_worktree_edits_do_not_change_approved_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); files = source_files(); commit = synthetic_git(root, files)
            (root / "src/genesis/__init__.py").write_bytes(b"# SYNTHETIC_CHANGED\n")
            self.assertEqual(release.read_commit(root, commit)["src/genesis/__init__.py"], files["src/genesis/__init__.py"])

    def test_git_symlink_mode_is_rejected_without_following_target(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); files = source_files(); synthetic_git(root, files)
            prefix = ["git", "-c", "safe.directory=" + str(root), "-C", str(root)]
            oid = subprocess.check_output(prefix + ["hash-object", "-w", "--stdin"], input=b"synthetic-target", timeout=10).decode().strip()
            subprocess.run(prefix + ["update-index", "--cacheinfo", "120000," + oid + ",src/genesis/__init__.py"], check=True, capture_output=True, timeout=10)
            tree = subprocess.check_output(prefix + ["write-tree"], timeout=10).decode().strip()
            commit = subprocess.check_output(prefix + ["-c", "user.name=Synthetic", "-c", "user.email=release@example.invalid",
                "commit-tree", tree, "-m", "Synthetic link"], timeout=10).decode().strip()
            with self.assertRaises(release.ReleaseError): release.read_commit(root, commit)

    def test_non_commit_and_mutable_refs_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); commit = synthetic_git(root, source_files())
            for ref in ("HEAD", "main", commit[:7], "../HEAD"):
                with self.assertRaises(release.ReleaseError): release.read_commit(root, ref)

    def test_snapshot_extra_files_empty_dirs_and_changed_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); files = source_files(); write_files(root, files)
            release.verify_snapshot(root, files)
            extra = root / "src/genesis/extra.py"; extra.write_bytes(b"# SYNTHETIC\n")
            with self.assertRaises(release.ReleaseError): release.verify_snapshot(root, files)
            extra.unlink(); (root / "unexpected").mkdir()
            with self.assertRaises(release.ReleaseError): release.verify_snapshot(root, files)
            (root / "unexpected").rmdir(); (root / "README.md").write_bytes(b"changed")
            with self.assertRaises(release.ReleaseError): release.verify_snapshot(root, files)

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / "sentinel").write_bytes(b"synthetic")
            with self.assertRaises(release.ReleaseError): release.prepare(root, "0" * 40, root, Path(sys.executable))
            self.assertEqual((root / "sentinel").read_bytes(), b"synthetic")

    def test_implicit_package_discovery_is_not_an_approved_release_config(self):
        files = source_files()
        self.assertEqual(release.configuration(files), ("0.2.0", ["genesis"]))
        files["pyproject.toml"] = files["pyproject.toml"].replace(b'packages = ["genesis"]', b'packages = ["genesis", "genesis.extra"]')
        with self.assertRaises(release.ReleaseError): release.configuration(files)

    def test_metadata_cannot_read_outside_approved_source(self):
        for readme in (b"../outside.md", b"C:/private.md", b"docs/README.md"):
            files = source_files(); files["pyproject.toml"] = files["pyproject.toml"].replace(b'readme = "README.md"', b'readme = "' + readme + b'"')
            with self.assertRaises(release.ReleaseError): release.configuration(files)

    def test_real_directory_link_is_rejected_without_reading_target(self):
        from test_data_root_guards import directory_link
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); target = root / "target"; target.mkdir()
            files = source_files(); write_files(target, files)
            link = root / "linked-source"; directory_link(link, target)
            with self.assertRaises(release.ReleaseError): release.verify_snapshot(link, files)
            with self.assertRaises(release.ReleaseError): release.prepare(target, "0" * 40, link / "output", Path(sys.executable))

    def test_output_in_git_repository_is_rejected_before_build(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); commit = synthetic_git(root, source_files())
            with self.assertRaises(release.ReleaseError): release.prepare(root, commit, root / "output", Path(sys.executable))
            self.assertFalse((root / "output").exists())

    def test_output_in_bare_git_metadata_is_rejected_before_build(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(["git", "init", "--bare", "--quiet", str(root)], check=True, capture_output=True, timeout=10)
            with self.assertRaises(release.ReleaseError): release.prepare(root, "0" * 40, root / "output", Path(sys.executable))
            self.assertFalse((root / "output").exists())

    def test_declared_source_size_and_binary_content_fail_closed(self):
        for data in (b"x" * (release.LIMIT + 1), b"synthetic\0binary", b"\xff"):
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder); files = source_files(); files["src/genesis/__init__.py"] = data
                commit = synthetic_git(root, files)
                with self.assertRaises(release.ReleaseError): release.read_commit(root, commit)


class ReleaseArchiveTests(unittest.TestCase):
    def test_exact_artifact_inventories_and_source_hashes_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); files = source_files(); archive_pair(root, files)
            report = release.verify_artifacts(root, files)
            self.assertEqual(len(report), 2)
            self.assertTrue(all(len(item["sha256"]) == 64 for item in report.values()))

    def test_wheel_extra_private_module_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); files = source_files(); archive_pair(root, files, extra_wheel={"genesis/extra_module.py": b"SYNTHETIC"})
            with self.assertRaises(release.ReleaseError): release.verify_artifacts(root, files)

    def test_sdist_extra_private_module_and_audit_are_rejected(self):
        for name in ("src/genesis/extra_module.py", "OPEN_SOURCE_READINESS_AUDIT.md", "src/genesis/data.json", ".env", "data.sqlite3", "profile.txt"):
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder); files = source_files(); archive_pair(root, files, extra_sdist={name: b"SYNTHETIC"})
                with self.assertRaises(release.ReleaseError): release.verify_artifacts(root, files)

    def test_changed_approved_module_is_rejected_even_with_valid_record(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); files = source_files(); archive_pair(root, files, change_wheel={"genesis/__init__.py": b"SYNTHETIC_CHANGED"})
            with self.assertRaises(release.ReleaseError): release.verify_artifacts(root, files)

    def test_missing_test_helper_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); files = source_files(); archive_pair(root, {p:v for p,v in files.items() if p != "tests/helper.py"})
            with self.assertRaises(release.ReleaseError): release.verify_artifacts(root, files)

    def test_extra_build_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); files = source_files(); archive_pair(root, files)
            (root / "unexpected.txt").write_bytes(b"SYNTHETIC")
            with self.assertRaises(release.ReleaseError): release.verify_artifacts(root, files)

    def test_archive_traversal_duplicates_and_case_collisions_are_rejected(self):
        for names in (("../escape.py",), ("/absolute.py",), ("C:/escape.py",), ("x.py", "X.py"), ("genesis//",)):
            with tempfile.TemporaryDirectory() as folder:
                p = Path(folder) / "synthetic.whl"
                with zipfile.ZipFile(p, "w") as z:
                    for name in names: z.writestr(name, b"SYNTHETIC")
                with self.assertRaises(release.ReleaseError): release._archive_entries(p)

    def test_archive_symlinks_and_tar_hardlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "synthetic.whl"
            with zipfile.ZipFile(p, "w") as z:
                i = zipfile.ZipInfo("module.py"); i.create_system = 3; i.external_attr = (stat.S_IFLNK | 0o777) << 16
                z.writestr(i, b"target.py")
            with self.assertRaises(release.ReleaseError): release._archive_entries(p)
            p = Path(folder) / "synthetic.tar.gz"
            with tarfile.open(p, "w:gz") as t:
                i = tarfile.TarInfo("module.py"); i.type = tarfile.LNKTYPE; i.linkname = "target.py"; t.addfile(i)
            with self.assertRaises(release.ReleaseError): release._archive_entries(p)

    def test_oversize_and_extra_empty_directory_are_rejected(self):
        for name, data in (("module.py", b"x" * (release.LIMIT + 1)), ("unexpected/", b"")):
            with tempfile.TemporaryDirectory() as folder:
                p = Path(folder) / "synthetic.whl"
                with zipfile.ZipFile(p, "w") as z: z.writestr(name, data)
                with self.assertRaises(release.ReleaseError): release._archive_entries(p)

    def test_record_checksum_corruption_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); files = source_files(); archive_pair(root, files)
            wheel, = root.glob("*.whl")
            with zipfile.ZipFile(wheel) as z: content = {n:z.read(n) for n in z.namelist()}
            record = next(n for n in content if n.endswith("/RECORD"))
            content[record] = content[record].replace(b"sha256=", b"sha512=", 1)
            with zipfile.ZipFile(wheel, "w") as z:
                for n,data in content.items(): z.writestr(n,data)
            with self.assertRaises(release.ReleaseError): release.verify_artifacts(root, files)

    def test_receipt_and_output_are_verified_against_pinned_source(self):
        with tempfile.TemporaryDirectory() as folder:
            temp = Path(folder); repo = temp / "repo"; repo.mkdir()
            files = source_files(); commit = synthetic_git(repo, files)
            output = temp / "output"; source = output / "source"; source.mkdir(parents=True)
            write_files(source, files); dist = output / "dist"; dist.mkdir(); archive_pair(dist, files)
            receipt = release._receipt(files, release.verify_artifacts(dist, files))
            (output / release.RECEIPT).write_text(json.dumps(receipt), encoding="utf-8")
            self.assertEqual(release.verify_preparation(repo, commit, output), receipt)
            receipt["files"]["README.md"]["sha256"] = "0" * 64
            (output / release.RECEIPT).write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaises(release.ReleaseError): release.verify_preparation(repo, commit, output)
