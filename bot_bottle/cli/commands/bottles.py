"""bottles: the bottle definitions this directory sees, and their versions.

    bot-bottle bottles                           list them
    bot-bottle bottles history <name>            its versions, newest first
    bot-bottle bottles history <name> <version>  what that version changed
    bot-bottle bottles revert <name> <version>   make it what it was then
    bot-bottle bottles migrate <name> [--yes]    rewrite old-format git-gate
                                                 repos as forge grants

A bottle comes from your host (`~/.bot-bottle/bottles/`), this project
(`./<name>`, from `./.bot-bottle/bottles/`, trusted per host) or bot-bottle
itself (built in); a host bottle shadows a built-in one of the same name.
Listed in a terminal as a table; piped, each is a tab-separated line:
<bottle>\\t<source>\\t<extends>\\t<trusted>\\t<problem> (`-` for an empty field).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ...contract.yaml_subset import YamlSubsetError, parse_frontmatter
from ...manifest import ManifestError, ManifestIndex
from ...manifest.loader import BUILTIN_BOTTLES_DIR, find_bottle, load_bottle_chain
from ...manifest.trust import UntrustedBottleError, check
from ..constants import PROG
from ..table import print_table

_HEADER = ("BOTTLE", "SOURCE", "EXTENDS", "TRUSTED", "PROBLEM")


def _extends(text: str) -> str:
    try:
        raw = parse_frontmatter(text)[0].get("extends")
    except YamlSubsetError:
        return ""
    names = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return ",".join(str(n) for n in names)


def _row(name: str, dirs: list[tuple[Path, bool]]) -> tuple[str, ...]:
    found = find_bottle(name, dirs)
    if found is None:
        return (name, "?", "", "", "not found")
    path, project = found
    source = ("project" if project else
              "built-in" if BUILTIN_BOTTLES_DIR in path.parents else "host")
    try:
        extends = _extends(path.read_text(encoding="utf-8"))
    except OSError:
        extends = ""
    trusted, problem = "", ""
    try:
        bottle, sources = load_bottle_chain(name, dirs)
        if project:
            check(name, bottle, sources)
            trusted = "yes"
    except UntrustedBottleError as e:
        trusted = "changed" if e.previous else "no"
    except (ManifestError, OSError) as e:
        problem = str(e).split("\n", 1)[0]
    return (name, source, extends, trusted, problem)


def cmd_bottles(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        sys.stderr.write(__doc__ or "")
        return 0
    if argv and argv[0] in ("history", "revert", "migrate"):
        # pylint: disable-next=import-outside-toplevel
        from .bottle import cmd_bottle
        return cmd_bottle(argv)
    if argv:
        sys.stderr.write(f"{PROG} bottles: unknown argument {argv[0]!r}\n\n{__doc__}")
        return 2
    index = ManifestIndex.resolve(os.getcwd(), missing_ok=True)
    if index.home_md is None:
        print("no bottles yet; `bot-bottle setup` writes the starters", file=sys.stderr)
        return 0
    dirs = index.bottle_dirs()
    print_table(_HEADER, [_row(name, dirs) for name in index.all_bottle_names], drop_empty=True)
    return 0


__all__ = ["cmd_bottles"]
