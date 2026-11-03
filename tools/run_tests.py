"""Run source-tree tests with the standard library, without installation."""

from pathlib import Path
import sys
import unittest


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    suite = unittest.defaultTestLoader.discover(str(root / "tests"))
    if suite.countTestCases() == 0:
        raise RuntimeError("no tests discovered")
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
