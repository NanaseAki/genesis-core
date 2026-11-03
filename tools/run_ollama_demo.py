"""Run the fixed synthetic HTTP demo directly from the source tree."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from genesis.adapters.ollama_demo import main

if __name__ == "__main__":
    raise SystemExit(main())
