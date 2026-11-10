"""attach: join a running agent session in this terminal via bot-bottle's tmux
server. Ctrl-] detaches; the agent keeps running.
"""

from __future__ import annotations

import os
import shutil
import sys

from ..constants import PROG
from ..launch_client import tmux_attach
from .active import held_bottles


def _resolve_session(name: str, held: dict[str, dict[str, object]]) -> str | None:
    """Resolve *name* to a running session id among the host controller's
    bottles (`held`).

    Accepts a UUID (or prefix) or a human label.  UUID prefix matching is
    unambiguous when only one session starts with the prefix; label matching
    finds the session whose bottled agent's label equals *name*
    (case-insensitive).  Returns None when nothing matches, and prints an
    error when multiple sessions are ambiguous."""
    running = {sid: str(b.get("label") or "") for sid, b in held.items()
               if isinstance(b.get("session"), dict)
               and b["session"].get("running")}  # type: ignore[union-attr]
    # Exact match — fastest, also handles UUIDs typed in full.
    if name in running:
        return name
    running_ids = set(running)
    # UUID prefix match (Docker-style: `attach abc123`).
    prefix_hits = [sid for sid in running_ids if sid.startswith(name)]
    if len(prefix_hits) == 1:
        return prefix_hits[0]
    if len(prefix_hits) > 1:
        sys.stderr.write(f"{PROG}: ambiguous prefix {name!r} matches: "
                         + ", ".join(sorted(prefix_hits)) + "\n")
        return None
    # Label match.
    name_lower = name.lower()
    label_hits = [sid for sid, label in running.items() if label.lower() == name_lower]
    if len(label_hits) == 1:
        return label_hits[0]
    if len(label_hits) > 1:
        sys.stderr.write(f"{PROG}: ambiguous label {name!r} matches: "
                         + ", ".join(sorted(label_hits)) + "\n")
        return None
    return None


def cmd_attach(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        sys.stderr.write(f"usage: {PROG} attach <session>\n")
        sys.stderr.write("\nJoin a running agent session; Ctrl-] detaches.\n"
                         "Accepts a UUID (or unique prefix) or the session's label.\n")
        return 0 if argv[:1] in (["-h"], ["--help"]) else 2
    name = argv[0]
    if shutil.which("tmux") is None:
        sys.stderr.write(f"{PROG}: tmux is not installed\n")
        return 1
    session = _resolve_session(name, held_bottles())
    if session is None:
        sys.stderr.write(f"{PROG}: no running session matching {name!r}\n")
        return 1
    argv_attach, env = tmux_attach(session)
    os.execve(argv_attach[0], argv_attach, env)
    return 1  # not reached
