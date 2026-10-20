"""PRD 0087's layer rules: services import only `contract`, `lib` and themselves;
`contract` and `lib` import only themselves and the stdlib.

Layers are assigned by path. Today's violations are listed in `ALLOWED`; each
refactor removes entries, and a stale entry fails the test so the list only
shrinks. Imports inside functions count."""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "bot_bottle"
SERVICES = ("orchestrator", "gateway")
STDLIB_ONLY = ("contract", "lib")

# (importer, imported) pairs that break the rules today. Remove entries as the
# modules move; never add one.
ALLOWED: frozenset[tuple[str, str]] = frozenset({
    ("bot_bottle.orchestrator.__main__", "bot_bottle.control.docker_broker"),
})


def module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def layer(module: str) -> str:
    """`contract`, `lib`, a service name, or `host`."""
    parts = module.split(".")
    if len(parts) > 1 and parts[1] in SERVICES + STDLIB_ONLY:
        return parts[1]
    return "host"


def imported_modules(path: Path) -> list[tuple[int, str]]:
    """(line, absolute module) for every import in `path`, at any depth."""
    module = module_name(path)
    is_package = path.name == "__init__.py"
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                parts = module.split(".")
                if not is_package:
                    parts = parts[:-1]
                parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join(parts + ([node.module] if node.module else []))
            if node.module is None:
                found.extend((node.lineno, f"{base}.{a.name}") for a in node.names)
            else:
                found.append((node.lineno, base))
    return found


def violations() -> list[tuple[str, str, str]]:
    """(importer, imported, location) for every import that breaks the rules."""
    out: list[tuple[str, str, str]] = []
    for path in sorted(PKG.rglob("*.py")):
        importer = module_name(path)
        source = layer(importer)
        if source == "host":
            continue
        for line, imported in imported_modules(path):
            where = f"{path.relative_to(ROOT)}:{line}"
            top = imported.split(".")[0]
            if top != "bot_bottle":
                if source in STDLIB_ONLY and top not in sys.stdlib_module_names:
                    out.append((importer, imported, where))
                continue
            if imported == "bot_bottle":
                continue
            target = layer(imported)
            allowed = {source} if source in STDLIB_ONLY else {source, *STDLIB_ONLY}
            if target not in allowed:
                out.append((importer, imported, where))
    return out


class TestLayering(unittest.TestCase):
    def test_no_new_layer_violations(self) -> None:
        new = sorted(
            f"{where}: {importer} -> {imported}"
            for importer, imported, where in violations()
            if (importer, imported) not in ALLOWED
        )
        self.assertEqual(
            [], new,
            "imports that break PRD 0087's layer rules (move the code, don't "
            "extend ALLOWED):\n" + "\n".join(new),
        )

    def test_allowed_list_has_no_stale_entries(self) -> None:
        current = {(importer, imported) for importer, imported, _ in violations()}
        stale = sorted(f"{a} -> {b}" for a, b in ALLOWED - current)
        self.assertEqual(
            [], stale,
            "these ALLOWED entries no longer occur; delete them:\n" + "\n".join(stale),
        )

    def test_layer_assignment(self) -> None:
        self.assertEqual("orchestrator", layer("bot_bottle.orchestrator.api"))
        self.assertEqual("gateway", layer("bot_bottle.gateway"))
        self.assertEqual("contract", layer("bot_bottle.contract.http"))
        self.assertEqual("host", layer("bot_bottle.cli.commands.start"))
        self.assertEqual("host", layer("bot_bottle.trust_domain"))


if __name__ == "__main__":
    unittest.main()
