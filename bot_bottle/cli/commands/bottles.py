"""bottles: the bottle definitions this directory sees, and their versions.

    bot-bottle bottles                           list them
    bot-bottle bottles history <name>            its versions, newest first
    bot-bottle bottles history <name> <version>  what that version changed
    bot-bottle bottles revert <name> <version>   make it what it was then
    bot-bottle bottles migrate <name> [--yes]    rewrite old-format git-gate
                                                 repos as forge grants
    bot-bottle bottles suggest "<task>"          propose a bottle for a task
      [--cwd DIR]                                  project directory (default: cwd)
      [--write NAME]                               save the new bottle as NAME
      [--rules]                                    use the rules path only (no
                                                   model; default: bottle-architect,
                                                   falling back to the rules)
      [--json]                                     print raw proposal JSON

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
from typing import TYPE_CHECKING

from ...contract.yaml_subset import YamlSubsetError, parse_frontmatter
from ...manifest import ManifestError, ManifestIndex
from ...manifest.loader import BUILTIN_BOTTLES_DIR, find_bottle, load_bottle_chain
from ...manifest.trust import UntrustedBottleError, check
from ..constants import PROG
from ..table import print_table
from ...suggest.render import render_proposal

if TYPE_CHECKING:
    from ...suggest.proposal import Proposal

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
    if argv and argv[0] == "suggest":
        return _cmd_suggest(argv[1:])
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


def _cmd_suggest(argv: list[str]) -> int:
    """Handle `bot-bottle bottles suggest "<task>" [options]`."""
    import argparse
    parser = argparse.ArgumentParser(prog=f"{PROG} bottles suggest")
    parser.add_argument("task", help="what the agent will do")
    parser.add_argument("--cwd", default=None,
                        help="project directory (default: current directory)")
    parser.add_argument("--write", metavar="NAME", default=None,
                        help="save a new bottle under this name")
    parser.add_argument("--rules", action="store_true",
                        help="use the rules path only: no model, the same answer every time")
    parser.add_argument("--json", action="store_true",
                        help="print raw proposal JSON to stdout")
    args = parser.parse_args(argv)

    cwd = Path(args.cwd) if args.cwd else Path(os.getcwd())
    # pylint: disable-next=import-outside-toplevel
    from ...suggest.flow import bottle_dirs, suggest
    from ...paths import bot_bottle_root  # pylint: disable=import-outside-toplevel
    dirs = bottle_dirs(cwd, bot_bottle_root())
    validated = suggest(args.task, cwd, rules=args.rules)

    print(render_proposal(validated, dirs, json_output=args.json))

    if args.write:
        return _write_suggested_bottle(args.write, validated, dirs, cwd)

    return 0


def _write_suggested_bottle(
    name: str,
    proposal: "Proposal",
    dirs: list[tuple[Path, bool]],
    cwd: Path,
) -> int:
    """Write the proposal as a new host bottle named `name`."""
    del dirs, cwd  # validated already; it's written to the host's bottles
    # pylint: disable=import-outside-toplevel
    from ...suggest.validate import is_stacked
    from ...suggest.write import SuggestWriteError, write_bottle

    try:
        path = write_bottle(name, proposal)
    except SuggestWriteError as e:
        sys.stderr.write(f"{PROG} bottles suggest --write: {e}\n")
        return 2
    print(f"wrote {path}", file=sys.stderr)
    if is_stacked(proposal) and proposal.bottles:
        # It holds only the additions: launch it stacked on the rest.
        flags = " ".join(f"--bottle {b}" for b in [*proposal.bottles, name])
        print(f"launch with: {PROG} start <agent> {flags}", file=sys.stderr)
    return 0


__all__ = ["cmd_bottles"]
