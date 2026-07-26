"""Architecture rules that should fail before coupling becomes entrenched."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class TestCliBackendBoundaries(unittest.TestCase):
    def test_cli_does_not_import_a_concrete_backend(self) -> None:
        forbidden = (
            "backend.docker", "backend.firecracker", "backend.macos_container",
            "bot_bottle.backend.docker", "bot_bottle.backend.firecracker",
            "bot_bottle.backend.macos_container",
        )
        violations: list[str] = []
        for path in (ROOT / "bot_bottle" / "cli").rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                module = node.module if isinstance(node, ast.ImportFrom) else None
                if module and module.startswith(forbidden):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: {module}")
                if isinstance(node, ast.Import):
                    violations.extend(
                        f"{path.relative_to(ROOT)}:{node.lineno}: {alias.name}"
                        for alias in node.names if alias.name.startswith(forbidden)
                    )
        self.assertEqual([], violations, "generic CLI imports concrete backend internals:\n" +
                         "\n".join(violations))


class TestRuntimeModuleSizes(unittest.TestCase):
    def test_egress_modules_stay_focused(self) -> None:
        caps = {
            "addon_core.py": 100,
            "schema.py": 400,
            "types.py": 180,
            "matching.py": 180,
            "dlp.py": 180,
            "context.py": 140,
        }
        directory = ROOT / "bot_bottle" / "gateway" / "egress"
        oversized = [f"{name} ({len((directory / name).read_text().splitlines())}>{cap})"
                     for name, cap in caps.items()
                     if len((directory / name).read_text().splitlines()) > cap]
        self.assertEqual([], oversized, "split a module rather than raising its cap: " +
                         ", ".join(oversized))

    def test_runtime_code_uses_focused_egress_modules(self) -> None:
        """addon_core is compatibility-only, never an internal dependency."""
        violations: list[str] = []
        package = ROOT / "bot_bottle"
        facade = package / "gateway" / "egress" / "addon_core.py"
        package_init = package / "gateway" / "egress" / "__init__.py"
        for path in package.rglob("*.py"):
            if path in (facade, package_init):
                continue
            text = path.read_text()
            if "gateway.egress.addon_core import" in text or \
                    ".addon_core import" in text:
                violations.append(str(path.relative_to(ROOT)))
        self.assertEqual([], violations)
