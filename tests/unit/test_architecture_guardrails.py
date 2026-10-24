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
                if isinstance(node, ast.ImportFrom):
                    module = node.module
                    if module and module.startswith(forbidden):
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}: {module}"
                        )
                if isinstance(node, ast.Import):
                    violations.extend(
                        f"{path.relative_to(ROOT)}:{node.lineno}: {alias.name}"
                        for alias in node.names if alias.name.startswith(forbidden)
                    )
        self.assertEqual([], violations, "generic CLI imports concrete backend internals:\n" +
                         "\n".join(violations))


CLI = ROOT / "bot_bottle" / "cli"
# The CLI modules that launch, list, resume or attach bottles: clients of the
# host controller (PRD 0092), which alone prepares and launches.
BOTTLE_CLIENT_MODULES = (
    CLI / "commands" / "start.py", CLI / "commands" / "resume.py",
    CLI / "commands" / "active.py", CLI / "commands" / "attach.py",
    CLI / "launch_client.py",
)
# (module, name imported from bot_bottle.backend) -> why it's still needed.
ALLOWED_BACKEND_IMPORTS: dict[tuple[str, str], str] = {
    ("start.py", "BottleSpec"): "--dry-run prepares locally, without the daemon",
    ("start.py", "get_bottle_backend"): "--dry-run prepares locally, without the daemon",
    ("start.py", "BottlePlan"): "the dry-run preflight renderer's parameter type",
    ("start.py", "launch_isolation_note"): "the dry-run preflight's docker boundary note",
    ("start.py", "enumerate_active_agents"): "label uniqueness against running bottles",
    ("active.py", "enumerate_active_agents"): "bottles the host controller doesn't hold",
}
# Bottle operations no client module may perform itself.
FORBIDDEN_CALLS = ("launch", "agent_command", "prelaunch_checks")
# `.prepare(...)` is allowed only in these functions.
PREPARE_ALLOWED_IN = {("start.py", "_dry_run")}


def _backend_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """(line, name) for every name imported from `bot_bottle.backend*`,
    relative or absolute, at any depth."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            stripped = module.removeprefix("bot_bottle.")
            if stripped == "backend" or stripped.startswith("backend."):
                found.extend((node.lineno, a.name) for a in node.names)
        elif isinstance(node, ast.Import):
            found.extend((node.lineno, a.name) for a in node.names
                         if a.name.startswith("bot_bottle.backend"))
    return found


class TestCliBottleCommandsAreHostControllerClients(unittest.TestCase):
    def test_no_backend_imports_beyond_the_allow_list(self) -> None:
        violations: list[str] = []
        for path in BOTTLE_CLIENT_MODULES:
            tree = ast.parse(path.read_text(), filename=str(path))
            violations.extend(
                f"{path.relative_to(ROOT)}:{line}: {name}"
                for line, name in _backend_imports(tree)
                if (path.name, name) not in ALLOWED_BACKEND_IMPORTS
            )
        self.assertEqual([], violations, "launch and attach go through the host controller; "
                         "don't import the backend for them:\n" + "\n".join(violations))

    def test_allow_list_has_no_stale_entries(self) -> None:
        used = {(path.name, name) for path in BOTTLE_CLIENT_MODULES
                for _, name in _backend_imports(ast.parse(path.read_text()))}
        self.assertEqual([], sorted(set(ALLOWED_BACKEND_IMPORTS) - used))

    def test_no_bottle_operations_in_client_modules(self) -> None:
        violations: list[str] = []
        for path in BOTTLE_CLIENT_MODULES:
            tree = ast.parse(path.read_text(), filename=str(path))
            functions = [n for n in ast.walk(tree)
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            prepare_ok = {id(node) for fn in functions
                          if (path.name, fn.name) in PREPARE_ALLOWED_IN for node in ast.walk(fn)}
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                attr = node.func.attr
                if attr in FORBIDDEN_CALLS or (attr == "prepare" and id(node) not in prepare_ok):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: .{attr}()")
        self.assertEqual([], violations, "bottle operations belong to the host controller:\n"
                         + "\n".join(violations))


class TestRuntimeModuleSizes(unittest.TestCase):
    def test_no_runtime_module_grows_beyond_global_ceiling(self) -> None:
        """A coarse ceiling catches new monoliths; focused caps stay tighter.
        Third-party code vendored verbatim (marked on its first line) is ours
        to ship, not to split."""
        ceiling = 850
        oversized = [
            f"{path.relative_to(ROOT)} ({len(path.read_text().splitlines())})"
            for path in (ROOT / "bot_bottle").rglob("*.py")
            if len(path.read_text().splitlines()) > ceiling
            and not path.read_text().startswith("# Vendored verbatim")
        ]
        self.assertEqual(
            [], oversized,
            f"runtime modules must stay at or below {ceiling} lines: "
            + ", ".join(oversized),
        )

    def test_egress_modules_stay_focused(self) -> None:
        caps = {
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

    def test_backend_contract_does_not_absorb_preparation_logic(self) -> None:
        caps = {
            ROOT / "bot_bottle" / "backend" / "base.py": 580,
            ROOT / "bot_bottle" / "backend" / "preparation.py": 160,
        }
        oversized = [
            f"{path.relative_to(ROOT)} "
            f"({len(path.read_text().splitlines())}>{cap})"
            for path, cap in caps.items()
            if len(path.read_text().splitlines()) > cap
        ]
        self.assertEqual([], oversized)
