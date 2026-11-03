"""Check the installed console script using two processes and temporary data."""
import os
from pathlib import Path
import subprocess
import sysconfig
import tempfile


def verify_outputs(first: str, second: str) -> None:
    left, right = first.splitlines(), second.splitlines()
    if (len(left) != 4 or len(right) != 4 or left[:3] != right[:3]
            or not left[0].startswith("Synthetic companion UUID: ")
            or not left[1].startswith("Day 0 SHA-256: ")
            or left[2] != "Fake response: synthetic persistence hello"
            or left[3] != "Model calls this process: 1"
            or right[3] != "Model calls this process: 0"):
        raise RuntimeError("installed persistence entry point did not preserve synthetic state")


def main() -> int:
    script = Path(sysconfig.get_path("scripts")) / (
        "genesis-persistence-demo.exe" if os.name == "nt" else "genesis-persistence-demo")
    if not script.is_file():
        raise RuntimeError("installed genesis-persistence-demo entry point is missing")
    with tempfile.TemporaryDirectory(prefix="genesis-entry-synthetic-") as folder:
        command = [str(script), "--data-root", str(Path(folder) / "data")]
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        outputs = [subprocess.run(command, check=True, capture_output=True, text=True,
                                  encoding="utf-8", env=env, timeout=30).stdout for _ in range(2)]
        verify_outputs(*outputs)
    print("Installed persistence console: two processes; stable UUID/Day 0/reply; calls 1 -> 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
