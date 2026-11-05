"""Execute the workflow's exact strict inventory/result blocks on synthetic logs."""
import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import ci_test_inventory as inventory


def workflow_block(name: str) -> str:
    lines = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8").splitlines()
    begin, end = f"# BEGIN {name}", f"# END {name}"
    starts = [i for i, line in enumerate(lines) if line.strip() == begin]
    stops = [i for i, line in enumerate(lines) if line.strip() == end]
    if len(starts) != 1 or len(stops) != 1 or starts[0] >= stops[0]:
        raise AssertionError("strict workflow markers missing or ambiguous")
    return textwrap.dedent("\n".join(lines[starts[0] + 1:stops[0]]))


def success_lines(ids: list[str]) -> list[str]:
    return [f"{name.rsplit('.', 1)[1]} ({name}) ... ok" for name in ids] + [
        "", "----------------------------------------------------------------------",
        f"Ran {len(ids)} tests in 1.234s", "", "OK",
        "Socket audit: 128 parent-process connection attempts, all literal loopback; no hostname lookup",
    ]


def run_workflow_cases(cases: list[dict]) -> list[dict]:
    """Use code extracted verbatim from YAML; external values are JSON data."""
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        raise RuntimeError("PowerShell is required to verify the Windows workflow parser")
    program = """$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$python = $env:GENESIS_CI_REGRESSION_PYTHON
$cases = @([IO.File]::ReadAllText($env:GENESIS_CI_REGRESSION_CASES) | ConvertFrom-Json)
$results = @(foreach ($case in $cases) {
  try {
    & {
      $snapshot = $case.snapshot
""" + workflow_block("STRICT TEST INVENTORY") + """
      if ($case.inventory_only) {
        [pscustomobject]@{name=$case.name; passed=$true; ids=@($expected)}
      } else {
        $lines = @($case.lines)
""" + workflow_block("STRICT TEST RESULT PARSER") + """
        [pscustomobject]@{name=$case.name; passed=$true}
      }
    }
  } catch {
    [pscustomobject]@{name=$case.name; passed=$false; error=$_.Exception.Message}
  }
})
ConvertTo-Json -InputObject @($results) -Depth 5 -Compress
"""
    with tempfile.TemporaryDirectory(prefix="genesis-ci-parser-synthetic-") as folder:
        data = Path(folder) / "cases.json"
        data.write_text(json.dumps(cases, ensure_ascii=True), encoding="utf-8")
        env = dict(os.environ, GENESIS_CI_REGRESSION_CASES=str(data),
                   GENESIS_CI_REGRESSION_PYTHON=sys.executable)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        completed = subprocess.run([shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", program],
                                   env=env, capture_output=True, text=True, encoding="utf-8",
                                   timeout=60, creationflags=flags)
        if completed.returncode != 0:
            raise AssertionError("workflow parser invocation failed: " + completed.stderr)
        results = json.loads(completed.stdout)
        if not isinstance(results, list) or len(results) != len(cases):
            raise AssertionError("workflow parser did not return every case")
        return results


class CiSnapshotTests(unittest.TestCase):
    def test_ast_inventory_ignores_plain_classes_and_nested_definitions(self):
        source = """import unittest
class First(unittest.TestCase):
    def test_one(self):
        class Nested(unittest.TestCase):
            def test_hidden(self): pass
        def test_nested_function(): pass
    def helper(self): pass
class Plain:
    def test_not_a_case(self): pass
class Second(unittest.TestCase):
    def test_two(self): pass
def test_free_function(): pass
"""
        with tempfile.TemporaryDirectory(prefix="genesis-ci-inventory-synthetic-") as folder:
            root = Path(folder)
            (root / "test_fixture.py").write_text(source, encoding="utf-8")
            self.assertEqual(inventory.test_ids(root), [
                "test_fixture.First.test_one", "test_fixture.Second.test_two"])

    def test_ast_inventory_rejects_ambiguous_or_unsupported_shapes(self):
        cases = (
            "class Case(unittest.TestCase):\n    def test_one(self): pass\n    def test_one(self): pass\n",
            "class Case(unittest.TestCase):\n    def test_one(self): pass\nclass Case: pass\n",
            "class Case(unittest.TestCase):\n    async def test_one(self): pass\n",
            "class Case(UnknownBase):\n    def test_one(self): pass\n",
            "class Case(unittest.TestCase):\n    def testCamel(self): pass\n",
            "class Case(unittest.TestCase):\n    def test_one(self): pass\ndef load_tests(a,b,c): pass\n",
        )
        with tempfile.TemporaryDirectory(prefix="genesis-ci-inventory-negative-") as folder:
            root = Path(folder)
            for source in cases:
                with self.subTest(source=source):
                    (root / "test_fixture.py").write_text("import unittest\n" + source, encoding="utf-8")
                    with self.assertRaises(inventory.InventoryError):
                        inventory.test_ids(root)

    def test_current_workflow_inventory_matches_unittest_discovery(self):
        expected = inventory.test_ids(ROOT / "tests")
        # Independent comparison only in this regression. The workflow helper
        # itself never imports tests or performs a second discovery/execution.
        def flatten(suite):
            for test in suite:
                if isinstance(test, unittest.TestSuite):
                    yield from flatten(test)
                else:
                    yield test.id()
        discovered = sorted(flatten(unittest.TestLoader().discover(str(ROOT / "tests"))))
        self.assertEqual(expected, discovered)
        results = run_workflow_cases([{"name": "current", "snapshot": str(ROOT), "inventory_only": True}])
        self.assertTrue(results[0]["passed"], results)
        self.assertEqual(sorted(results[0]["ids"]), discovered)

    def test_exact_workflow_parser_accepts_current_inventory_and_known_layouts(self):
        ids = inventory.test_ids(ROOT / "tests")
        plain = success_lines(ids)
        described = list(plain)
        described[0:1] = [plain[0].removesuffix(" ... ok"), "Synthetic description ... ok"]
        warning = "warning: in the working copy of 'README.md', LF will be replaced by CRLF the next time Git touches it"
        warned = list(plain)
        for suffix in ("test_commit_replacement_does_not_change_export", "test_tree_replacement_does_not_change_export"):
            name = "test_release_replacement.ReplacementSecurityTests." + suffix
            index = warned.index(f"{suffix} ({name}) ... ok")
            warned[index:index + 1] = [f"{suffix} ({name}) ... {warning}", "ok"]
        cases = [{"name": name, "snapshot": str(ROOT), "lines": lines}
                 for name, lines in (("plain", plain), ("description", described), ("known-warning", warned))]
        results = run_workflow_cases(cases)
        self.assertTrue(all(result["passed"] for result in results), results)

    def test_exact_workflow_parser_rejects_drift_skip_warning_and_bad_summaries(self):
        ids = inventory.test_ids(ROOT / "tests")
        valid = success_lines(ids)
        summary = f"Ran {len(ids)} tests in 1.234s"
        unknown = "test_extra (test_unknown.Unknown.test_extra) ... ok"
        variants = {
            "old-289-summary": ["Ran 289 tests in 1.234s" if line == summary else line for line in valid],
            "missing-id": valid[1:],
            "duplicate-id": [valid[0]] + valid,
            "skipped": [valid[0].replace(" ... ok", " ... skipped 'synthetic'")] + valid[1:],
            "failed": [valid[0].replace(" ... ok", " ... FAIL")] + valid[1:],
            "unknown-id": [unknown] + valid[1:],
            "unknown-output": valid + ["synthetic unexpected output"],
            "unapproved-warning": valid + ["warning: synthetic unknown warning"],
            "description-warning": [valid[0].removesuffix(" ... ok"), "warning: synthetic description ... ok"] + valid[1:],
            "duplicate-summary": valid + [summary],
            "missing-ok": [line for line in valid if line != "OK"],
            "duplicate-ok": valid + ["OK"],
        }
        cases = [{"name": name, "snapshot": str(ROOT), "lines": lines} for name, lines in variants.items()]
        results = run_workflow_cases(cases)
        self.assertTrue(all(not result["passed"] for result in results), results)

    def test_workflow_rejects_added_test_and_same_count_renamed_id(self):
        ids = inventory.test_ids(ROOT / "tests")
        with tempfile.TemporaryDirectory(prefix="genesis-ci-inventory-drift-") as folder:
            roots = [Path(folder) / "added", Path(folder) / "renamed"]
            for root in roots:
                (root / "tests").mkdir(parents=True)
                (root / "tools").mkdir()
                (root / "tools/ci_test_inventory.py").write_bytes((ROOT / "tools/ci_test_inventory.py").read_bytes())
                for file in (ROOT / "tests").glob("test_*.py"):
                    (root / "tests" / file.name).write_bytes(file.read_bytes())
            (roots[0] / "tests/test_extra.py").write_text(
                "import unittest\nclass Extra(unittest.TestCase):\n    def test_added(self): pass\n", encoding="utf-8")
            module, _, method = ids[0].split(".")
            renamed = roots[1] / "tests" / (module + ".py")
            source = renamed.read_text(encoding="utf-8")
            source, changed = re.subn(r"\bdef " + re.escape(method) + r"\(", "def test_renamed_inventory_drift(", source, count=1)
            self.assertEqual(changed, 1)
            ast.parse(source)
            renamed.write_text(source, encoding="utf-8")
            cases = [
                {"name": "added", "snapshot": str(roots[0]), "inventory_only": True},
                {"name": "same-count-renamed", "snapshot": str(roots[1]), "lines": success_lines(ids)},
            ]
            results = run_workflow_cases(cases)
            self.assertTrue(all(not result["passed"] for result in results), results)


if __name__ == "__main__":
    unittest.main()
