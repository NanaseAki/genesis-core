"""Run source or installed tests with a parent-process socket audit guard.

This is verification instrumentation, not a production sandbox. Fresh HTTP
restart workers install their own guard; other children only run local Git,
SQLite and FakeModel scenarios. It does not attest OS-wide network activity.
"""
import argparse
import ipaddress
from pathlib import Path
import sys
import unittest

CONNECTIONS: list[str] = []


def guard(event: str, args: tuple) -> None:
    if event in {"socket.gethostbyaddr", "socket.getnameinfo"}:
        raise RuntimeError("test attempted reverse hostname lookup")
    if event in {"socket.connect", "socket.sendto", "socket.getaddrinfo", "socket.gethostbyname"}:
        if event in {"socket.getaddrinfo", "socket.gethostbyname"}:
            host = args[0]
        elif event == "socket.sendto":
            host = args[-1][0]
        else:
            host = args[1][0]
        try:
            allowed = ipaddress.ip_address(host).is_loopback
        except ValueError:
            allowed = False
        if not allowed:
            raise RuntimeError("test attempted non-loopback networking or hostname lookup")
        if event == "socket.connect":
            CONNECTIONS.append(str(host))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if not args.installed:
        sys.path.insert(0, str(root / "src"))
    sys.addaudithook(guard)
    suite = unittest.defaultTestLoader.discover(str(root / "tests"))
    if suite.countTestCases() == 0:
        raise RuntimeError("no tests discovered")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"Socket audit: {len(CONNECTIONS)} parent-process connection attempts, all literal loopback; no hostname lookup")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
