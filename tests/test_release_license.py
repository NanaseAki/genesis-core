"""Apache license provenance attacks; public license text, otherwise synthetic data."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import test_release_prepare as fixture
import test_release_security as attacks
import release_rules as rules

release = fixture.release
INFO = "project_genesis-0.2.0.dist-info/"


def wheel_change(dist, change):
    attacks.rewrite_wheel(next(dist.glob("*.whl")), change)


def sdist_change(dist, change):
    path = next(dist.glob("*.gz"))
    with tarfile.open(path) as archive:
        entries = {item.name: archive.extractfile(item).read() for item in archive if item.isfile()}
    change(entries)
    with tarfile.open(path, "w:gz", format=tarfile.GNU_FORMAT) as archive:
        for name, data in entries.items():
            item = tarfile.TarInfo(name); item.size = len(data)
            archive.addfile(item, io.BytesIO(data))


def metadata_change(dist, change):
    def transform(entries):
        entries[INFO + "METADATA"] = change(entries[INFO + "METADATA"])
    wheel_change(dist, transform)


class LicenseProvenanceTests(unittest.TestCase):
    def check_attack(self, mutate, *, cli=False):
        with tempfile.TemporaryDirectory(prefix="genesis-license-test-") as temporary:
            root = Path(temporary); repo = root / "repo"; repo.mkdir()
            files = fixture.source_files(); commit = fixture.synthetic_git(repo, files)
            output = root / "checked"; source = output / "source"; source.mkdir(parents=True)
            fixture.write_files(source, files); dist = output / "dist"; dist.mkdir()
            fixture.archive_pair(dist, files)
            mutate(dist)
            # Attacker updates RECORD (wheel helper) and every artifact/receipt hash.
            (output / release.RECEIPT).write_text(json.dumps(attacks.forged_receipt(files, dist)), encoding="utf-8")
            with self.assertRaises(release.ReleaseError):
                release.verify_artifacts(dist, files)
            with self.assertRaises(release.ReleaseError):
                release.verify_preparation(repo, commit, output)
            with patch.object(release, "check_backend", side_effect=AssertionError("backend executed")) as backend:
                with self.assertRaises(release.ReleaseError):
                    release.verify_roundtrip(dist, files, Path(sys.executable))
                backend.assert_not_called()
            if cli:
                tool = Path(__file__).resolve().parents[1] / "tools/release_prepare.py"
                args = [str(tool), "--repository", str(repo), "--commit", commit, "--output", str(output),
                        "--build-python", sys.executable, "--verify-only", "--roundtrip", "--private-preparation"]
                actual = subprocess.run([sys.executable, "-B", "-I", "-S", *args], capture_output=True, timeout=25)
                self.assertNotEqual(actual.returncode, 0)
                self.assertIn(b"ReleaseError", actual.stderr)
                # Instrument the same CLI main in a child, observing backend entry rather than just exit status.
                marker = root / "backend-called"
                code = ("import sys\nfrom pathlib import Path\nsys.path.insert(0," + repr(str(tool.parent)) + ")\n"
                        "import release_prepare as r\n"
                        "def forbidden_backend(p):\n    Path(" + repr(str(marker)) + ").write_text('called')\n"
                        "    raise AssertionError('backend executed')\n"
                        "r.check_backend=forbidden_backend\nsys.argv=" + repr(args) + "\nr.main()\n")
                observed = subprocess.run([sys.executable, "-B", "-I", "-S", "-c", code], capture_output=True, timeout=25)
                self.assertNotEqual(observed.returncode, 0)
                self.assertIn(b"ReleaseError", observed.stderr)
                self.assertFalse(marker.exists(), "CLI invoked backend on invalid candidate")

    def test_official_bytes_and_approved_metadata_positive(self):
        files = fixture.source_files()
        self.assertEqual(hashlib.sha256(files["LICENSE"]).hexdigest(), rules.APACHE_LICENSE_SHA256)
        self.assertEqual(rules.approved_licenses(files), ("LICENSE",))
        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary); fixture.archive_pair(dist, files)
            artifacts = release.verify_artifacts(dist, files)
            with zipfile.ZipFile(next(dist.glob("*.whl"))) as archive:
                self.assertEqual(archive.read(INFO + "licenses/LICENSE"), files["LICENSE"])
                metadata = archive.read(INFO + "METADATA")
                self.assertIn(b"License-Expression: Apache-2.0\n", metadata)
                self.assertIn(b"License-File: LICENSE\n", metadata)
                self.assertIn(b"Dynamic: license-file\n", metadata)
            self.assertEqual(len(artifacts), 2)

    def test_source_license_missing_changed_or_empty_rejected(self):
        for data in (None, b"", b"SYNTHETIC_REPLACED_LICENSE", fixture.source_files()["LICENSE"] + b" "):
            with self.subTest(data_size=None if data is None else len(data)):
                files = fixture.source_files()
                if data is None: del files["LICENSE"]
                else: files["LICENSE"] = data
                with self.assertRaises(release.ReleaseError): release.configuration(files)

    def test_spdx_values_and_wrong_types_rejected(self):
        for value in ('"MIT"', '"Apache-2.0 OR MIT"', '""', 'true', '7', '{file="LICENSE"}'):
            files = fixture.source_files()
            files["pyproject.toml"] = files["pyproject.toml"].replace(b'license = "Apache-2.0"', ("license = " + value).encode())
            with self.subTest(value=value), self.assertRaises(release.ReleaseError): release.configuration(files)

    def test_license_file_paths_duplicates_and_types_rejected(self):
        for value in ('[]', '["LICENSE","LICENSE"]', '["LICENSE","NOTICE"]', '["license"]',
                      '["../LICENSE"]', '["/LICENSE"]', '["C:/LICENSE"]', '["licenses/LICENSE"]',
                      '["LICENSE", "license"]', '["LICENSE*"]', 'true', '7', '"LICENSE"'):
            files = fixture.source_files()
            files["pyproject.toml"] = files["pyproject.toml"].replace(b'license-files = ["LICENSE"]', ("license-files = " + value).encode())
            with self.subTest(value=value), self.assertRaises(release.ReleaseError): release.configuration(files)

    def test_removing_both_static_declarations_is_not_legacy_success(self):
        files = fixture.source_files()
        files["pyproject.toml"] = files["pyproject.toml"].replace(b'license = "Apache-2.0"\nlicense-files = ["LICENSE"]\n', b'')
        with self.assertRaises(release.ReleaseError): release.configuration(files)

    def test_unapproved_source_notice_rejected(self):
        files = fixture.source_files(); files["NOTICE"] = b"SYNTHETIC_UNAPPROVED_NOTICE"
        with self.assertRaises(release.ReleaseError): release.configuration(files)

    def test_wheel_missing_license_rejected_with_recomputed_receipt(self):
        self.check_attack(lambda d: wheel_change(d, lambda e: e.pop(INFO + "licenses/LICENSE")), cli=True)

    def test_wheel_replaced_license_rejected_with_recomputed_record(self):
        self.check_attack(lambda d: wheel_change(d, lambda e: e.update({INFO + "licenses/LICENSE": b"SYNTHETIC_LICENSE"})))

    def test_extra_notice_and_case_collision_members_rejected(self):
        for name in (INFO + "licenses/NOTICE", INFO + "licenses/license", INFO + "licenses/private.py"):
            with self.subTest(name=name):
                self.check_attack(lambda d: wheel_change(d, lambda e: e.update({name: b"SYNTHETIC_UNAPPROVED"})))

    def test_wrong_license_expression_metadata_rejected(self):
        self.check_attack(lambda d: metadata_change(d, lambda v: v.replace(b"License-Expression: Apache-2.0", b"License-Expression: MIT")))

    def test_duplicate_license_file_header_rejected(self):
        self.check_attack(lambda d: metadata_change(d, lambda v: v.replace(b"License-File: LICENSE\n", b"License-File: LICENSE\nLicense-File: LICENSE\n")), cli=True)

    def test_missing_license_file_header_rejected(self):
        self.check_attack(lambda d: metadata_change(d, lambda v: v.replace(b"License-File: LICENSE\n", b"")))

    def test_unknown_and_escaping_license_headers_rejected(self):
        for value in (b"NOTICE", b"../LICENSE", b"/LICENSE", b"C:/LICENSE", b"license"):
            with self.subTest(value=value):
                self.check_attack(lambda d: metadata_change(d, lambda v: v.replace(b"License-File: LICENSE\n", b"License-File: " + value + b"\n")))

    def test_extra_license_file_and_expression_headers_rejected(self):
        for field in (b"License-File: NOTICE\n", b"License-Expression: Apache-2.0\n", b"License: Apache-2.0\n"):
            with self.subTest(field=field):
                self.check_attack(lambda d: metadata_change(d, lambda v: field + v))

    def test_dynamic_only_exact_observed_license_file_allowed(self):
        for field in (b"", b"Dynamic: author-email\n", b"Dynamic: license-expression\n", b"Dynamic: license-file\nDynamic: license-file\n"):
            with self.subTest(field=field):
                self.check_attack(lambda d: metadata_change(d, lambda v: v.replace(b"Dynamic: license-file\n", field)))

    def test_sdist_license_content_mismatch_rejected_before_build(self):
        root = "project_genesis-0.2.0/"
        for case in ("replaced", "missing", "extra_notice"):
            def change(entries):
                if case == "replaced": entries[root + "LICENSE"] = b"SYNTHETIC_OTHER_TEXT"
                elif case == "missing": entries.pop(root + "LICENSE")
                else: entries[root + "NOTICE"] = b"SYNTHETIC_UNAPPROVED_NOTICE"
            with self.subTest(case=case):
                self.check_attack(lambda d: sdist_change(d, change), cli=case == "replaced")

    def test_both_sdist_pkg_info_copies_bound_to_approved_license(self):
        for suffix in ("/project_genesis-0.2.0/PKG-INFO", "/src/project_genesis.egg-info/PKG-INFO"):
            # Root path has no leading slash; select separately to avoid suffix ambiguity.
            suffix = "project_genesis-0.2.0/PKG-INFO" if suffix.startswith("/project_genesis") else suffix
            with self.subTest(suffix=suffix):
                self.check_attack(lambda d: attacks.rewrite_sdist(next(d.glob("*.gz")), suffix,
                    lambda v: v.replace(b"License-Expression: Apache-2.0", b"License-Expression: MIT")))

    def test_fixed_git_license_blob_cannot_be_replaced_in_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repo = root / "repo"; repo.mkdir()
            files = fixture.source_files(); commit = fixture.synthetic_git(repo, files)
            source = root / "snapshot"; source.mkdir(); fixture.write_files(source, files)
            pinned = release.read_commit(repo, commit)
            (source / "LICENSE").write_bytes(b"SYNTHETIC_CHANGED")
            with self.assertRaises(release.ReleaseError): release.verify_snapshot(source, pinned)
            self.assertEqual(pinned["LICENSE"], files["LICENSE"])

    def test_record_license_hash_and_size_checked_independently(self):
        def mutate(dist):
            path = next(dist.glob("*.whl"))
            with zipfile.ZipFile(path) as archive: entries = {n: archive.read(n) for n in archive.namelist()}
            entries[INFO + "RECORD"] = entries[INFO + "RECORD"].replace(b"licenses/LICENSE,sha256=", b"licenses/LICENSE,sha512=")
            with zipfile.ZipFile(path, "w") as archive:
                for name, data in entries.items(): archive.writestr(name, data)
        self.check_attack(mutate)
