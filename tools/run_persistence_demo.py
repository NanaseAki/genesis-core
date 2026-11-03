"""Source-tree entry point; all input is fixed synthetic text."""

from pathlib import Path
import sys


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from genesis.adapters.persistence_demo import main

    raise SystemExit(main())
