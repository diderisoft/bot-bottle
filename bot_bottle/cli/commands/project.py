"""bot-bottle project: the project checkouts this host shows the console.

  bot-bottle project add [path]     keep the checkout at path (default: here)
  bot-bottle project list           the checkouts kept: path, repo, branch, last used
  bot-bottle project remove [path]  forget the checkout at path (default: here)

The console lists a project for each repo its hosts keep a checkout of, and
can list a checkout's bottles at any branch. A launch from a project's
directory keeps its checkout too; one that's gone from disk drops off. A
checkout is a git work tree with a remote (`origin`, or its only one).
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from ... import host_access
from ...contract.hostctl import PROJECTS_PATH, PROJECTS_REMOVE_PATH
from ...control.hostctl.runner import HostControlError
from ..constants import PROG
from ..table import print_table

_HEADER = ("PATH", "REPO", "BRANCH", "LAST USED")


def _when(iso: object) -> str:
    """An ISO 8601 UTC time as local `YYYY-MM-DD HH:MM`; "" for none."""
    if not isinstance(iso, str):
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime(
            "%Y-%m-%d %H:%M")
    except ValueError:
        return ""


def _list() -> int:
    found = host_access.ask(PROJECTS_PATH).get("checkouts")
    rows = [(str(c.get("path", "")), str(c.get("repo", "")), str(c.get("branch", "")),
             _when(c.get("last_used")) or "never")
            for c in (found if isinstance(found, list) else []) if isinstance(c, dict)]
    if not rows:
        sys.stderr.write(f"no checkouts kept on this host; `{PROG} project add` keeps one\n")
        return 0
    print_table(_HEADER, rows)
    return 0


def _add(path: str) -> int:
    reply = host_access.ask(PROJECTS_PATH, {"path": path})
    checkout = reply.get("checkout")
    repo = checkout.get("repo", "") if isinstance(checkout, dict) else ""
    sys.stderr.write(f"keeping {path} ({repo}) for the console\n")
    if reply.get("note"):
        sys.stderr.write(f"note: {reply['note']}\n")
    return 0


def _remove(path: str) -> int:
    if host_access.ask(PROJECTS_REMOVE_PATH, {"path": path}).get("removed"):
        sys.stderr.write(f"forgot {path}\n")
        return 0
    sys.stderr.write(f"{PROG}: {path} isn't a kept checkout\n")
    return 1


def cmd_project(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        sys.stderr.write(__doc__ or "")
        return 0 if argv else 2
    action, rest = argv[0], argv[1:]
    if action not in ("add", "list", "remove") or len(rest) > (0 if action == "list" else 1):
        sys.stderr.write(f"usage: {PROG} project add|remove [path], or {PROG} project list\n")
        return 2
    try:
        if action == "list":
            return _list()
        path = str(Path(rest[0] if rest else ".").expanduser().resolve())
        return _add(path) if action == "add" else _remove(path)
    except HostControlError as e:
        sys.stderr.write(f"{PROG}: {getattr(e, 'error', '') or e}\n")
        return 1


__all__ = ["cmd_project"]
