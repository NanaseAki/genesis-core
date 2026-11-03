"""Synthetic independent provenance regressions, including rewritten receipts."""
import base64
import csv
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import test_release_prepare as fixture
import release_rules as rules

release = fixture.release


def directory_wrapped_sdist(path, *, nonzero=True, owner=False, pax=None):
    """Keep all regular bytes, varying only the directory/header interpretation."""
    with tarfile.open(path) as archive:
        entries = {x.name: archive.extractfile(x).read() for x in archive if x.isfile()}
    inner = io.BytesIO(); length = 0
    with tarfile.open(fileobj=inner, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for name, data in entries.items():
            member = tarfile.TarInfo(name); member.size = len(data)
            if owner and name.endswith("/README.md"):
                member.uname = "SYNTHETIC_TAR_OWNER_719"
            archive.addfile(member, io.BytesIO(data))
            length += 512 + ((len(data) + 511) // 512) * 512
    directory = tarfile.TarInfo(path.name.removesuffix(".tar.gz"))
    directory.type = tarfile.DIRTYPE; directory.mode = 0o755
    directory.size = length if nonzero else 0
    directory.pax_headers = pax or {}
    header = directory.tobuf(format=tarfile.PAX_FORMAT if pax else tarfile.GNU_FORMAT)
    path.write_bytes(gzip.compress(header + inner.getvalue()[:length] + b"\0" * 1024))
    return entries


def rewrite_wheel(path, transform, comment=b""):
    with zipfile.ZipFile(path) as archive:
        entries = {n: archive.read(n) for n in archive.namelist()}
    transform(entries)
    record = next(n for n in entries if n.endswith("/RECORD"))
    rows = io.StringIO(); writer = csv.writer(rows, lineterminator="\n")
    for name, data in entries.items():
        writer.writerow([name, "", ""] if name == record else [name, "sha256=" +
            base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode(), len(data)])
    entries[record] = rows.getvalue().encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        archive.comment = comment


def rewrite_sdist(path, suffix, data):
    with tarfile.open(path) as archive:
        entries = {x.name: archive.extractfile(x).read() for x in archive if x.isfile()}
    name = next(n for n in entries if n.endswith(suffix))
    entries[name] = data(entries[name]) if callable(data) else data
    with tarfile.open(path, "w:gz", format=tarfile.GNU_FORMAT) as archive:
        for name, content in entries.items():
            item = tarfile.TarInfo(name); item.size = len(content)
            archive.addfile(item, io.BytesIO(content))


def forged_receipt(files, dist):
    """Attacker recomputes all hashes independently, without calling validation."""
    artifacts = {}
    for path in dist.iterdir():
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as archive:
                entries = {n: archive.read(n) for n in archive.namelist()}
        else:
            with tarfile.open(path) as archive:
                entries = {x.name: archive.extractfile(x).read() for x in archive if x.isfile()}
        artifacts[path.name] = {"sha256": release.digest(path.read_bytes()), "files": sorted(entries),
                                "content_sha256": {n: release.digest(v) for n, v in sorted(entries.items())}}
    return release._receipt(files, artifacts)


class MetadataSecurityTests(unittest.TestCase):
    def test_approved_console_and_project_metadata_are_preserved(self):
        files = fixture.source_files()
        files["pyproject.toml"] += (b'\n[project.scripts]\ngenesis-demo = "genesis.adapters.console:main"\n')
        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary); fixture.archive_pair(dist, files)
            report = release.verify_artifacts(dist, files)
            wheel = next(dist.glob("*.whl"))
            with zipfile.ZipFile(wheel) as archive:
                points = next(n for n in archive.namelist() if n.endswith("/entry_points.txt"))
                self.assertEqual(archive.read(points), b"[console_scripts]\ngenesis-demo = genesis.adapters.console:main\n")
            self.assertIn(wheel.name, report)

    def test_metadata_line_endings_do_not_change_approved_source_bytes(self):
        files = fixture.source_files()
        files["README.md"] = "Synthetic 中文\r\nsecond line\r\n".encode()
        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary); fixture.archive_pair(dist, files)
            release.verify_artifacts(dist, files)
            with tarfile.open(next(dist.glob("*.gz"))) as archive:
                name = next(n for n in archive.getnames() if n.endswith("/README.md"))
                self.assertEqual(archive.extractfile(name).read(), files["README.md"])

    def test_release_cli_works_without_site_or_backend(self):
        tool = Path(__file__).resolve().parents[1] / "tools/release_prepare.py"
        import sys
        result = subprocess.run([sys.executable, "-B", "-I", "-S", str(tool), "--help"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--roundtrip", result.stdout)

    def check_rewritten_receipt(self, transform):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repo = root / "repo"; repo.mkdir()
            files = fixture.source_files(); commit = fixture.synthetic_git(repo, files)
            output = root / "output"; source = output / "source"; source.mkdir(parents=True)
            fixture.write_files(source, files); dist = output / "dist"; dist.mkdir()
            fixture.archive_pair(dist, files)
            transform(dist)
            receipt = forged_receipt(files, dist)
            (output / release.RECEIPT).write_text(json.dumps(receipt), encoding="utf-8")
            # Both hashes and member counts are self-consistent. Provenance must still fail.
            with self.assertRaises(release.ReleaseError):
                release.verify_preparation(repo, commit, output)
            # The roundtrip entry must reject it before starting even the backend probe.
            with patch.object(release.subprocess, "run", side_effect=AssertionError("backend executed")):
                with self.assertRaises(release.ReleaseError):
                    release.verify_roundtrip(dist, files, Path("synthetic-build-python"))

    def test_rewritten_wheel_console_and_receipt_rejected(self):
        def mutate(dist):
            rewrite_wheel(next(dist.glob("*.whl")), lambda e: e.update({next(n for n in e if n.endswith(
                "/entry_points.txt")): b"[console_scripts]\ngenesis-demo = genesis.day0:initial_state\n"}))
        self.check_rewritten_receipt(mutate)

    def test_external_data_files_rejected_before_any_backend_execution(self):
        def mutate(dist):
            rewrite_sdist(next(dist.glob("*.gz")), "/setup.cfg",
                          b"[egg_info]\ntag_build = \ntag_date = 0\n\n[options.data_files]\n. = ../synthetic.txt\n")
        # read-only verification must not invoke a build on the candidate sdist.
        original_run = release.subprocess.run
        def git_only(command, *args, **kwargs):
            self.assertEqual(command[0], "git", "candidate executed before validation")
            return original_run(command, *args, **kwargs)
        # Create fixture before restricting subprocesses (synthetic_git invokes Git too).
        with patch.object(release.subprocess, "run", side_effect=git_only):
            self.check_rewritten_receipt(mutate)

    def test_rewritten_sources_inventory_and_receipt_rejected(self):
        self.check_rewritten_receipt(lambda d: rewrite_sdist(next(d.glob("*.gz")), "/SOURCES.txt",
                                                            lambda v: v + b"synthetic_outside.py\n"))

    def test_zip_comment_and_rewritten_receipt_rejected(self):
        self.check_rewritten_receipt(lambda d: rewrite_wheel(next(d.glob("*.whl")), lambda e: None,
                                                            b"SYNTHETIC_HIDDEN_COMMENT"))

    def test_unapproved_metadata_headers_dependency_license_identity_rejected(self):
        for field in (b"Requires-Dist: synthetic\n", b"License: SYNTHETIC_UNAUTHORIZED\n",
                      b"Author-email: synthetic@example.invalid\n", b"X-Synthetic: payload\n"):
            def mutate(dist):
                def change(entries):
                    key = next(n for n in entries if n.endswith("/METADATA"))
                    entries[key] = field + entries[key]
                rewrite_wheel(next(dist.glob("*.whl")), change)
            with self.subTest(field=field): self.check_rewritten_receipt(mutate)

    def test_wheel_generator_and_extra_tag_rejected(self):
        for value in (b"Wheel-Version: 1.0\nGenerator: synthetic\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
                      b"Wheel-Version: 1.0\nGenerator: setuptools (84.1.0)\nRoot-Is-Purelib: true\nTag: py3-none-any\nTag: synthetic\n\n"):
            def mutate(dist):
                rewrite_wheel(next(dist.glob("*.whl")), lambda e: e.update({next(n for n in e if n.endswith("/WHEEL")): value}))
            with self.subTest(value=value): self.check_rewritten_receipt(mutate)

    def test_egg_metadata_and_dependency_links_rejected(self):
        for suffix, data in (("/dependency_links.txt", b"synthetic://external\n"),
                             ("/top_level.txt", b"genesis\nsynthetic\n"),
                             ("/entry_points.txt", b"[other]\nsynthetic = x:y\n")):
            with self.subTest(suffix=suffix):
                self.check_rewritten_receipt(lambda d: rewrite_sdist(next(d.glob("*.gz")), suffix, data))

    def test_metadata_readme_cannot_carry_unapproved_text(self):
        def mutate(dist):
            rewrite_wheel(next(dist.glob("*.whl")), lambda e: e.update({next(n for n in e if n.endswith(
                "/METADATA")): b"Metadata-Version: 2.4\nName: project-genesis\nVersion: 0.2.0\n"
                               b"Description-Content-Type: text/markdown\n\nSYNTHETIC_UNAPPROVED_BODY\n"}))
        self.check_rewritten_receipt(mutate)

    def test_sources_duplicates_and_execution_sections_rejected(self):
        for suffix, change in (("/SOURCES.txt", lambda v: v + b"README.md\n"),
                               ("/setup.cfg", lambda v: v + b"[options.package_data]\n* = ../synthetic\n"),
                               ("/setup.cfg", lambda v: v + b"[aliases]\ninstall = synthetic\n")):
            with self.subTest(suffix=suffix):
                self.check_rewritten_receipt(lambda d: rewrite_sdist(next(d.glob("*.gz")), suffix, change))

    def test_tar_extra_owner_payload_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.tar.gz"
            with tarfile.open(path, "w:gz") as archive:
                member = tarfile.TarInfo("module.py"); member.uname = "SYNTHETIC_UNAPPROVED_OWNER"
                archive.addfile(member, io.BytesIO(b""))
            with self.assertRaises(release.ReleaseError): release._archive_entries(path)

    def test_zip_prefix_tail_extra_and_gzip_concatenation_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); fixture.archive_pair(root, fixture.source_files())
            wheel = next(root.glob("*.whl")); original = wheel.read_bytes()
            for data in (b"SYNTHETIC_PREFIX" + original, original + b"SYNTHETIC_TAIL"):
                wheel.write_bytes(data)
                with self.assertRaises(release.ReleaseError): release._archive_entries(wheel)
            wheel.write_bytes(original)
            with zipfile.ZipFile(wheel, "a") as z:
                z.comment = b"synthetic"
            with self.assertRaises(release.ReleaseError): release._archive_entries(wheel)
            sdist = next(root.glob("*.gz")); sdist.write_bytes(sdist.read_bytes() + gzip.compress(b"SYNTHETIC"))
            with self.assertRaises(release.ReleaseError): release._archive_entries(sdist)

class TarDirectorySecurityTests(unittest.TestCase):
    def test_nonzero_directory_rejected_before_hidden_record_parsing(self):
        for pax, parsed_headers in ((None, 1), ({"mtime": "1.0"}, 2)):
            with self.subTest(pax=pax), tempfile.TemporaryDirectory() as temporary:
                dist = Path(temporary); fixture.archive_pair(dist, fixture.source_files())
                path = next(dist.glob("*.gz")); directory_wrapped_sdist(path, pax=pax)
                raw = path.read_bytes()
                with patch.object(tarfile.TarInfo, "frombuf", wraps=tarfile.TarInfo.frombuf) as parse:
                    with self.assertRaises(release.ReleaseError):
                        rules._tar(raw, path.name.removesuffix(".gz"))
                    self.assertEqual(parse.call_count, parsed_headers)

    def test_upper_directory_check_does_not_depend_on_container_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary); fixture.archive_pair(dist, fixture.source_files())
            path = next(dist.glob("*.gz")); directory_wrapped_sdist(path)
            with patch.object(release, "verify_container"), patch.object(
                    tarfile.TarFile, "extractfile", side_effect=AssertionError("hidden record read")) as read:
                with self.assertRaises(release.ReleaseError):
                    release._archive_entries(path)
                read.assert_not_called()

    def test_owner_wrapper_with_unchanged_members_and_rewritten_receipt_rejected(self):
        def mutate(dist):
            path = next(dist.glob("*.gz")); before = directory_wrapped_sdist(path, owner=True)
            with tarfile.open(path) as archive:
                members = list(archive)
                after = {x.name: archive.extractfile(x).read() for x in members if x.isfile()}
            self.assertEqual(before, after)
            self.assertGreater(members[0].size, 0)
            self.assertTrue(any(x.uname == "SYNTHETIC_TAR_OWNER_719" for x in members))
        MetadataSecurityTests.check_rewritten_receipt(self, mutate)

    def test_verify_only_cli_rejects_wrapper_and_recomputed_receipt(self):
        import sys
        tool = Path(__file__).resolve().parents[1] / "tools/release_prepare.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repo = root / "repo"; repo.mkdir()
            files = fixture.source_files(); commit = fixture.synthetic_git(repo, files)
            output = root / "output"; source = output / "source"; source.mkdir(parents=True)
            fixture.write_files(source, files); dist = output / "dist"; dist.mkdir()
            fixture.archive_pair(dist, files)
            receipt_path = output / release.RECEIPT
            receipt_path.write_text(json.dumps(forged_receipt(files, dist)), encoding="utf-8")
            command = [sys.executable, "-B", "-I", "-S", str(tool), "--repository", str(repo),
                       "--commit", commit, "--output", str(output), "--verify-only", "--private-preparation"]
            normal = subprocess.run(command, capture_output=True, timeout=30)
            self.assertEqual(normal.returncode, 0, normal.stderr)
            path = next(dist.glob("*.gz")); before = directory_wrapped_sdist(path, owner=True)
            with tarfile.open(path) as archive:
                self.assertEqual(before, {x.name: archive.extractfile(x).read() for x in archive if x.isfile()})
            receipt_path.write_text(json.dumps(forged_receipt(files, dist)), encoding="utf-8")
            rejected = subprocess.run(command, capture_output=True, timeout=30)
            self.assertNotEqual(rejected.returncode, 0)

    def test_roundtrip_rejects_wrapper_without_calling_backend(self):
        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary); files = fixture.source_files(); fixture.archive_pair(dist, files)
            directory_wrapped_sdist(next(dist.glob("*.gz")), owner=True)
            with patch.object(release, "check_backend", side_effect=AssertionError("backend called")) as backend:
                with self.assertRaises(release.ReleaseError):
                    release.verify_roundtrip(dist, files, Path("not-executed"))
                backend.assert_not_called()

    def test_zero_size_directory_preserves_files_with_optional_mtime_pax(self):
        for pax in (None, {"mtime": "1.0"}):
            with self.subTest(pax=pax), tempfile.TemporaryDirectory() as temporary:
                dist = Path(temporary); files = fixture.source_files(); fixture.archive_pair(dist, files)
                path = next(dist.glob("*.gz")); before = directory_wrapped_sdist(path, nonzero=False, pax=pax)
                self.assertEqual(release._archive_entries(path), before)
                release.verify_artifacts(dist, files)

    def test_pax_directory_size_override_fails_closed_in_both_layers(self):
        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary); fixture.archive_pair(dist, fixture.source_files())
            path = next(dist.glob("*.gz"))
            directory_wrapped_sdist(path, nonzero=False, pax={"size": "4096"})
            with tarfile.open(path) as archive:
                self.assertEqual(next(iter(archive)).size, 4096)
            with self.assertRaises(release.ReleaseError):
                rules._tar(path.read_bytes(), path.name.removesuffix(".gz"))
            with patch.object(release, "verify_container"), patch.object(tarfile.TarFile, "extractfile") as read:
                with self.assertRaises(release.ReleaseError):
                    release._archive_entries(path)
                read.assert_not_called()
