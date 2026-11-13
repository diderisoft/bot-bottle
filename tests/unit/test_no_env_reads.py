"""PRD 0092: daemon-side host code reads no environment to decide what to do.

Behaviour knobs arrive as `Settings` (built by the CLI's loader,
`bot_bottle/cli/settings.py`) and secret values from a `SecretSource`
(`bot_bottle/cli/secrets_env.py`). Every remaining `os.environ` / `os.getenv` read
in the scanned modules must be in `ALLOWED`, keyed by file and what it reads
(the variable name, the name of the constant or variable holding it, or
`os.environ` for the whole environment). Entries only ever shrink: a stale one
fails the test."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "bot_bottle"

SCANNED_DIRS = ("backend", "contrib", "control", "egress", "git_gate")
SCANNED_FILES = (
    "agent_image.py", "agent_overlay.py", "agent_provider.py", "bottle_state.py",
    "env.py", "image_cache.py", "image_inputs.py", "image_registry.py",
    "infra_image.py", "paths.py", "published_artifacts.py", "resources.py",
    "secret_source.py", "util.py", "workspace.py",
)

_REGISTRY_TOKEN = "optional registry token for private registries; deferred"
_CHILD_ENV = "process plumbing: the child process inherits this process's env"
_FORWARDED_BASE = (
    f"{_CHILD_ENV}; forwarded secret values come from the plan, not this env")
_CREDENTIAL_PATHS = "process plumbing: HOME / CODEX_HOME locate the user's credential files"

# (file relative to bot_bottle/, what it reads) -> why it may.
ALLOWED: dict[tuple[str, str], str] = {
    # Registry auth tokens.
    ("image_registry.py", "TOKEN_ENV"): _REGISTRY_TOKEN,
    ("image_registry.py", "TOKEN_HOST_ENV"):
        f"{_REGISTRY_TOKEN}: the one host the token may go to",
    # Bottle launch: the base env for `docker compose` / `container run`.
    ("backend/docker/launch.py", "os.environ"): _FORWARDED_BASE,
    ("backend/macos_container/launch.py", "os.environ"): _FORWARDED_BASE,
    ("contrib/claude/claude_auth.py", "os.environ"): _CREDENTIAL_PATHS,
    ("contrib/codex/codex_auth.py", "os.environ"): _CREDENTIAL_PATHS,
    # Process plumbing.
    ("paths.py", "BOT_BOTTLE_ROOT"): "process plumbing: relocates the state root",
    ("paths.py", "BOT_BOTTLE_FC_CACHE"): "process plumbing: relocates the firecracker cache root",
    ("util.py", "HOME"): "process plumbing: the invoking user's home",
    ("backend/util.py", "HOME"): "process plumbing: the invoking user's home",
    ("backend/firecracker/setup.py", "SUDO_USER"): "process plumbing: the invoking user",
    ("backend/firecracker/setup.py", "USER"): "process plumbing: the invoking user",
    ("backend/macos_container/bottle.py", "name"):
        "process plumbing: forwards the terminal's TERM-family vars into `container exec`",
    ("backend/macos_container/bottle.py", "os.environ"): _CHILD_ENV,
    ("backend/docker/bottle.py", "os.environ"): _CHILD_ENV,
    ("backend/macos_container/pty_forward.py", "os.environ"): _CHILD_ENV,
    ("backend/docker/gateway.py", "os.environ"): _CHILD_ENV,
    ("control/hostctl/upgrade_link.py", "os.environ"):
        f"{_CHILD_ENV}: the installer an upgrade runs, less what it installs (ADR 0022)",
    ("backend/docker/orchestrator.py", "os.environ"): _CHILD_ENV,
    ("backend/macos_container/gateway.py", "os.environ"): _CHILD_ENV,
    ("backend/macos_container/orchestrator.py", "os.environ"): _CHILD_ENV,
    ("control/hostctl/runner.py", "os.environ"): _CHILD_ENV,
    ("control/hostctl/launch.py", "os.environ"): _CHILD_ENV,
    ("control/hostctl/architect_bottle.py", "os.environ"): _CHILD_ENV,
    ("control/hostctl/__main__.py", "PORT_ENV"):
        "process plumbing: the daemon's bind port, set by whoever starts it",
}


def scanned_paths() -> list[Path]:
    paths = [p for d in SCANNED_DIRS for p in (PKG / d).rglob("*.py")]
    return sorted(paths + [PKG / name for name in SCANNED_FILES])


def _key_of(node: ast.expr | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return "?"


def env_reads(path: Path) -> list[tuple[int, str]]:
    """(line, what) for every environment read in `path`."""
    tree = ast.parse(path.read_text(), filename=str(path))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            found.extend((node.lineno, f"from os import {a.name}") for a in node.names
                         if a.name in ("environ", "getenv"))
        if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == "os" and node.attr in ("environ", "getenv")):
            continue
        parent = parents.get(node)
        what = "os.environ"
        if node.attr == "getenv" and isinstance(parent, ast.Call):
            what = _key_of(parent.args[0] if parent.args else None)
        elif isinstance(parent, ast.Subscript):
            what = _key_of(parent.slice)
        elif isinstance(parent, ast.Attribute) and parent.attr in ("get", "setdefault"):
            call = parents.get(parent)
            if isinstance(call, ast.Call):
                what = _key_of(call.args[0] if call.args else None)
        found.append((node.lineno, what))
    return sorted(found)


def all_reads() -> dict[tuple[str, str], list[int]]:
    out: dict[tuple[str, str], list[int]] = {}
    for path in scanned_paths():
        rel = path.relative_to(PKG).as_posix()
        for line, what in env_reads(path):
            out.setdefault((rel, what), []).append(line)
    return out


class TestNoEnvReads(unittest.TestCase):
    def test_no_unlisted_env_reads(self) -> None:
        new = sorted(
            f"bot_bottle/{rel}:{lines[0]}: {what}"
            for (rel, what), lines in all_reads().items()
            if (rel, what) not in ALLOWED
        )
        self.assertEqual(
            [], new,
            "host code reads the environment; take the value from `Settings` "
            "(bot_bottle/cli/settings.py fills it) or, for a secret, from the "
            "plan's `SecretSource` instead:\n" + "\n".join(new),
        )

    def test_allowed_has_no_stale_entries(self) -> None:
        stale = sorted(f"{rel}: {what}" for rel, what in set(ALLOWED) - set(all_reads()))
        self.assertEqual([], stale, "these ALLOWED entries no longer occur; delete them:\n"
                         + "\n".join(stale))

    def test_detects_each_read_form(self) -> None:
        import tempfile
        src = (
            "import os\n"
            "a = os.environ.get('A')\n"
            "b = os.environ['B']\n"
            "c = os.getenv('C')\n"
            "d = dict(os.environ)\n"
            "e = os.environ.get(NAME)\n"
            "from os import environ\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.py"
            path.write_text(src)
            whats = [what for _, what in env_reads(path)]
        self.assertEqual(
            ["A", "B", "C", "os.environ", "NAME", "from os import environ"], whats)


if __name__ == "__main__":
    unittest.main()
