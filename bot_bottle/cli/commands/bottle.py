"""A bottle's versions (PRD 0103), behind `bot-bottle bottles history|revert`,
and `bottles migrate` for a bottle in an old format (git-gate repos, or a
route's auth written with `scheme`, `header` or `token_ref`).

  bot-bottle bottles history <name>             its versions, newest first
  bot-bottle bottles history <name> <version>   what that version changed
  bot-bottle bottles revert <name> <version>    make it what it was then
  bot-bottle bottles migrate <name> [--yes]     rewrite its old-format repos
                                                as grants on a forge bottle,
                                                and old-format route auth as
                                                type and value

Every change to a host bottle (`~/.bot-bottle/bottles/`) is a version, hand
edits included. A project bottle's versions are the project's commits; a
revert there is left for you to commit. A revert takes effect at the next
launch, and is itself a version you can revert.

`migrate` turns repos whose keys a forge mints, written in long form (`url`,
`key: {provider: gitea, ...}`, `host_key`), into one-line grants on a forge
bottle (`bot-bottle forge connect` writes one), adding it to `extends`, and
a route's old-format auth into `type` and `value`: `scheme: Bearer` (or
`token`) becomes `type: bearer` (or `token`), `header: X` becomes `type:
header, name: X`, and `token_ref: NAME` becomes `value: secret:NAME` (a
`header` with a `scheme` is left for you to rewrite). It shows the change and
asks first (`--yes` doesn't), and writes it as a new version, so
`revert` undoes it. Everything else in the file stays as it is.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from ...lib.log import die, info
from ...manifest import ManifestIndex
from ...manifest.loader import BUILTIN_BOTTLES_DIR
from ...manifest.migrate import MigrateError, Migration, apply, prepare
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


def _migrate(args: list[str]) -> int:
    yes = "--yes" in args
    names = [a for a in args if a != "--yes"]
    if len(names) != 1 or names[0].startswith("-"):
        die(f"usage: {PROG} bottles migrate <name> [--yes]")
    dirs = ManifestIndex.resolve(os.getcwd()).bottle_dirs()
    try:
        m = prepare(names[0], dirs)
    except MigrateError as e:
        die(str(e))
    return _write_migration(m, dirs, yes)


def _write_migration(m: Migration, dirs: list[tuple[Path, bool]], yes: bool) -> int:
    sys.stdout.write(m.diff())
    for note in m.notes:
        info(f"note: {note}")
    for change in m.changes:
        info(f"changed: {change}")
    if not yes:
        if not sys.stdin.isatty():
            die("not writing it without a terminal to confirm; pass --yes")
        if input(f"write this to {m.path}? [y/N] ").strip().lower() not in ("y", "yes"):
            info("left as it was")
            return 1
    try:
        sha = apply(m, dirs)
    except (MigrateError, VersionError) as e:
        die(str(e))
    info(f"migrated {m.bottle}"
         + (f" (now {sha[:9]}; `{PROG} bottles revert {m.bottle} <version>` undoes it)"
            if sha else "; commit it with the project"))
    return 0


def cmd_bottle(argv: list[str]) -> int:
    if argv and argv[0] == "migrate":
        return _migrate(argv[1:])
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in ("history", "revert"):
        sys.stderr.write(__doc__ or "")
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    verb, args = argv[0], argv[1:]
    if not args or (verb == "revert" and len(args) < 2):
        die(f"usage: {PROG} bottles {verb} <name>{' <version>' if verb == 'revert' else ''}")
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
