"""bot-bottle bottle: a bottle's versions (PRD 0103).

  bot-bottle bottle history <name>             its versions, newest first
  bot-bottle bottle history <name> <version>   what that version changed
  bot-bottle bottle revert <name> <version>    make it what it was then

Every change to a host bottle (`~/.bot-bottle/bottles/`) is a version, hand
edits included. A project bottle's versions are the project's commits; a
revert there is left for you to commit. A revert takes effect at the next
launch, and is itself a version you can revert.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from ...lib.log import die, info
from ...manifest import ManifestIndex
from ...manifest.loader import BUILTIN_BOTTLES_DIR
from ...manifest.versions import VersionError, history, is_dirty, revert, show
from ..constants import PROG


def _bottle_path(name: str) -> Path:
    index = ManifestIndex.resolve(os.getcwd())
    for d, _project in index.bottle_dirs():
        path = d / f"{name}.md"
        if path.is_file():
            if d == BUILTIN_BOTTLES_DIR:
                die(f"{name} is built in; it has no history")
            return path
    die(f"bottle '{name}' not found")


def _print_history(path: Path) -> None:
    print(path)
    if is_dirty(path):
        print("  (uncommitted changes)")
    for v in history(path):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(v.when))
        print(f"  {v.sha[:9]}  {when}  {v.message}")


def cmd_bottle(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in ("history", "revert"):
        sys.stderr.write(__doc__ or "")
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    verb, args = argv[0], argv[1:]
    if not args or (verb == "revert" and len(args) < 2):
        die(f"usage: {PROG} bottle {verb} <name>{' <version>' if verb == 'revert' else ''}")
    path = _bottle_path(args[0])
    try:
        if verb == "history" and len(args) == 1:
            _print_history(path)
        elif verb == "history":
            sys.stdout.write(show(path, args[1]))
        else:
            sha = revert(path, args[1])
            info(f"reverted {args[0]} to {args[1]}"
                 + (f" (now {sha[:9]})" if sha else "; commit it with the project")
                 + "; it applies at the next launch")
    except VersionError as e:
        die(str(e))
    return 0
