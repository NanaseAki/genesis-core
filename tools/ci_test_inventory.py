"""Read the locked unittest layout with AST, without importing or running tests.

Supported cases are top-level classes directly deriving from unittest.TestCase.
Only their direct synchronous test_ methods count; plain helper classes and
nested definitions do not. Unsupported discovery hooks and ambiguous test
shapes fail closed. The workflow separately pins the count and verifies every
reported ID, so this is not an evaluator for arbitrary dynamic Python code.
"""
import argparse
import ast
import json
from pathlib import Path
import re


class InventoryError(ValueError):
    pass


def test_ids(tests: Path) -> list[str]:
    if not tests.is_dir():
        raise InventoryError("test directory is missing")
    files = sorted(tests.glob("test_*.py"))
    if not files or any(path.parent != tests for path in tests.rglob("test_*.py")):
        raise InventoryError("missing or unsupported nested test modules")
    result: list[str] = []
    for path in files:
        if not re.fullmatch(r"test_[A-Za-z0-9_]+", path.stem):
            raise InventoryError("unsupported test module name")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        seen_classes: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "load_tests":
                raise InventoryError("dynamic discovery hooks are not supported")
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name in seen_classes:
                raise InventoryError("duplicate top-level class name")
            seen_classes.add(node.name)
            methods = [child for child in node.body
                       if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and child.name.startswith("test")]
            direct_case = (len(node.bases) == 1 and isinstance(node.bases[0], ast.Attribute)
                           and isinstance(node.bases[0].value, ast.Name)
                           and node.bases[0].value.id == "unittest"
                           and node.bases[0].attr == "TestCase")
            if not direct_case:
                if methods and node.bases:
                    raise InventoryError("unsupported test class inheritance")
                continue  # A plain helper is not discovered by unittest.
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", node.name):
                raise InventoryError("unsupported test class name")
            names: set[str] = set()
            for method in methods:
                if (isinstance(method, ast.AsyncFunctionDef)
                        or not re.fullmatch(r"test_[A-Za-z0-9_]+", method.name)):
                    raise InventoryError("unsupported test method shape")
                if method.name in names:
                    raise InventoryError("duplicate test method name")
                names.add(method.name)
                result.append(f"{path.stem}.{node.name}.{method.name}")
    if not result or len(result) != len(set(result)):
        raise InventoryError("empty or duplicate test inventory")
    return sorted(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests", type=Path, required=True)
    args = parser.parse_args()
    try:
        ids = test_ids(args.tests)
    except (InventoryError, OSError, SyntaxError, UnicodeError):
        parser.exit(1, "Unsupported or unreadable static test inventory.\n")
    print(json.dumps(ids, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
