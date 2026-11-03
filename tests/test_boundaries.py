"""Architectural boundaries and an offline smoke scenario with synthetic data."""

import ast
from contextlib import redirect_stdout
from io import StringIO
from importlib.util import resolve_name
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import UUID

from genesis.adapters.console import main
from genesis.core import GenesisCore, UserInput
from genesis.identity import CompanionIdentity
from genesis.models.fake import FakeModel


def import_dependencies(source: str, module: str, *, is_package: bool = False) -> frozenset[str]:
    """Resolve static import targets without importing or executing their code."""
    package = module if is_package else module.rpartition(".")[0]
    dependencies: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            dependencies.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                raise AssertionError("wildcard imports cannot be checked statically")
            try:
                base = resolve_name("." * node.level + (node.module or ""), package)
            except ImportError as error:
                raise AssertionError("relative import escapes its package") from error
            dependencies.add(base)
            dependencies.update(f"{base}.{alias.name}" for alias in node.names)
    return frozenset(dependencies)


def assert_import_boundaries(source: str, module: str, *, is_package: bool = False) -> None:
    boundaries = {
        "models": {"core", "identity", "memory", "growth", "policy", "adapters", "storage", "day0", "data_paths"},
        "memory": {"core", "models", "growth", "policy", "adapters", "storage", "day0", "data_paths"},
        "growth": {"core", "models", "policy", "adapters", "storage", "day0", "data_paths"},
        "policy": {"core", "models", "memory", "growth", "adapters", "storage", "day0", "data_paths"},
        "identity": {"core", "models", "memory", "growth", "policy", "adapters", "storage", "day0", "data_paths"},
        "core": {"adapters", "storage", "day0", "data_paths"},
        "storage": {"adapters", "growth"},
    }
    parts = module.split(".")
    layer = parts[1] if len(parts) > 1 else ""
    for dependency in import_dependencies(source, module, is_package=is_package):
        target = dependency.split(".")
        if target[0] not in sys.stdlib_module_names | {"genesis"}:
            raise AssertionError(f"third-party import: {dependency}")
        if target[0] == "genesis" and len(target) > 1:
            if target[1] in boundaries.get(layer, set()):
                raise AssertionError(f"forbidden dependency: {module} -> {dependency}")
            if layer == "core" and (
                dependency == "genesis.models.fake" or dependency.startswith("genesis.models.fake.")
            ):
                raise AssertionError("Core must depend on the model contract, not FakeModel")
        if layer in {"models", "core", "memory", "growth", "policy", "identity"} and target[0] == "sqlite3":
            raise AssertionError("SQLite implementation must stay in the persistence layer")


def source_module(path: Path, source: Path) -> tuple[str, bool]:
    parts = path.relative_to(source).with_suffix("").parts
    is_package = parts[-1] == "__init__"
    return ".".join(("genesis",) + (parts[:-1] if is_package else parts)), is_package


class BoundaryTests(unittest.TestCase):
    def test_production_imports_use_only_stdlib_and_genesis(self) -> None:
        source = Path(__file__).resolve().parents[1] / "src" / "genesis"
        for path in source.rglob("*.py"):
            module, is_package = source_module(path, source)
            for name in import_dependencies(path.read_text(encoding="utf-8"), module,
                                            is_package=is_package):
                with self.subTest(path=path.name, dependency=name):
                    self.assertIn(name.split(".")[0], sys.stdlib_module_names | {"genesis"})

    def test_model_and_domain_layers_do_not_import_state_owners_or_adapters(self) -> None:
        source = Path(__file__).resolve().parents[1] / "src" / "genesis"
        for path in source.rglob("*.py"):
            module, is_package = source_module(path, source)
            with self.subTest(module=module):
                assert_import_boundaries(path.read_text(encoding="utf-8"), module,
                                         is_package=is_package)

    def test_guard_rejects_forbidden_absolute_relative_and_aliased_imports(self) -> None:
        for module, source in (
            ("genesis.models.fake", "import genesis.core as c"),
            ("genesis.models.fake", "from genesis import core"),
            ("genesis.models.fake", "from genesis import memory as m"),
            ("genesis.models.fake", "from genesis.core import GenesisCore as C"),
            ("genesis.models.fake", "from .. import policy as p"),
            ("genesis.models.fake", "from ..memory import MemoryRecord"),
            ("genesis.core", "import genesis.models.fake as fake"),
            ("genesis.core", "from genesis.models import fake as f"),
            ("genesis.core", "from .models import fake"),
            ("genesis.core", "from . import adapters"),
            ("genesis.core", "import nonstdlib_review_fixture as third_party"),
            ("genesis.models.fake", "from ..storage import sqlite as db"),
            ("genesis.core", "from .storage.sqlite import SQLiteStore"),
            ("genesis.growth", "import sqlite3 as db"),
        ):
            with self.subTest(module=module, source=source):
                with self.assertRaises(AssertionError):
                    assert_import_boundaries(source, module)

    def test_guard_allows_resolved_contract_imports_and_package_context(self) -> None:
        for module, source, is_package in (
            ("genesis.models.fake", "from .contracts import Model as M", False),
            ("genesis.models", "from . import contracts", True),
            ("genesis.core", "from .models import contracts as c", False),
            ("genesis.core", "from .models.contracts import Model", False),
            ("genesis.growth", "from .memory import MemoryRecord", False),
        ):
            with self.subTest(module=module, source=source):
                assert_import_boundaries(source, module, is_package=is_package)
        self.assertIn("genesis.memory", import_dependencies("from .. import memory as m",
                                                          "genesis.models.fake"))

    def test_guard_explicitly_rejects_unresolvable_or_wildcard_imports(self) -> None:
        for source in ("from ... import core", "from genesis import *"):
            with self.subTest(source=source), self.assertRaises(AssertionError):
                assert_import_boundaries(source, "genesis.core")

    def test_synthetic_flow_needs_no_network_files_or_processes(self) -> None:
        with (
            patch("socket.socket", side_effect=AssertionError("network forbidden")),
            patch("builtins.open", side_effect=AssertionError("file access forbidden")),
            patch("subprocess.Popen", side_effect=AssertionError("process launch forbidden")),
        ):
            core = GenesisCore(CompanionIdentity(UUID(int=1)), FakeModel())
            session = core.create_session(UUID(int=2))
            turn = core.send(session.session_id, UserInput(UUID(int=3), "synthetic offline"))
            self.assertEqual(turn.assistant.content, "Fake response: synthetic offline")

    def test_console_demo_only_uses_fixed_synthetic_input(self) -> None:
        output = StringIO()
        with patch.object(sys, "argv", ["genesis-demo"]), redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertEqual(output.getvalue(), "Fake response: synthetic hello\n")
