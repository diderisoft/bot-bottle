"""cleanup: stop and remove all orphaned claude-bottle resources
(containers + networks) left behind by previous bottles, plus
optionally the per-bottle state dirs under ~/.claude-bottle/state/.

State cleanup is prompted separately from container cleanup because
the trade-off is different: containers + networks are pure debris,
but a state dir may carry a resumable bottle (capability-block
rebuild + transcript snapshot) the operator still wants."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .. import supervise as _supervise
from ..backend import get_bottle_backend
from ..log import info
from ._common import read_tty_line


def cmd_cleanup(_argv: list[str]) -> int:
    backend = get_bottle_backend()
    plan = backend.prepare_cleanup()
    state_dirs = _enumerate_state_dirs()

    if plan.empty and not state_dirs:
        info("no claude-bottle resources to clean up")
        return 0

    if not plan.empty:
        plan.print()
        if _prompt_yes("remove all of the above?"):
            backend.cleanup(plan)
            info("containers + networks: cleaned")
        else:
            info("containers + networks: skipped")

    if state_dirs:
        _print_state(state_dirs)
        if _prompt_yes(
            "remove per-bottle state? (loses resumable bottles)",
        ):
            for d in state_dirs:
                shutil.rmtree(d, ignore_errors=True)
            info(f"state: removed {len(state_dirs)} dir(s)")
        else:
            info("state: skipped")

    return 0


# --- State enumeration + display ------------------------------------------


def _enumerate_state_dirs() -> list[Path]:
    """All per-bottle state dirs under ~/.claude-bottle/state/.
    Sorted for stable preflight output."""
    state_root = _supervise.claude_bottle_root() / "state"
    if not state_root.is_dir():
        return []
    return sorted(p for p in state_root.iterdir() if p.is_dir())


def _state_summary(path: Path) -> str:
    """One-line description suitable for the cleanup prompt. Calls
    out resumability so the operator can decide whether removing it
    loses anything they care about."""
    flags: list[str] = []
    if (path / "metadata.json").is_file():
        flags.append("resumable")
    else:
        flags.append("no metadata.json (orphan)")
    if (path / "Dockerfile").is_file():
        flags.append("rebuilt Dockerfile")
    if (path / "transcript").is_dir():
        flags.append("transcript snapshot")
    if (path / ".preserve").is_file():
        flags.append("preserve marker")
    return f"state:     {path.name} ({', '.join(flags)})"


def _print_state(dirs: list[Path]) -> None:
    print(file=sys.stderr)
    for d in dirs:
        info(_state_summary(d))
    print(file=sys.stderr)


# --- Prompt ----------------------------------------------------------------


def _prompt_yes(message: str) -> bool:
    sys.stderr.write(f"claude-bottle: {message} [y/N] ")
    sys.stderr.flush()
    reply = read_tty_line()
    return reply in ("y", "Y", "yes", "YES")
