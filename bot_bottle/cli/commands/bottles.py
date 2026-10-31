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
      [--architect]                                use bottle-architect (default:
                                                   rules path)
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

from ...contract.yaml_subset import YamlSubsetError, parse_frontmatter
from ...manifest import ManifestError, ManifestIndex
from ...manifest.loader import BUILTIN_BOTTLES_DIR, find_bottle, load_bottle_chain
from ...manifest.trust import UntrustedBottleError, check
from ..constants import PROG
from ..table import print_table
from ...suggest.render import render_proposal
from ...suggest.validate import proposal_to_frontmatter, validate_proposal

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
    parser.add_argument("--architect", action="store_true",
                        help="use bottle-architect instead of the rules path")
    parser.add_argument("--json", action="store_true",
                        help="print raw proposal JSON to stdout")
    args = parser.parse_args(argv)

    cwd = Path(args.cwd) if args.cwd else Path(os.getcwd())

    index = ManifestIndex.resolve(str(cwd), missing_ok=True)
    dirs = index.bottle_dirs() if index.home_md else [(BUILTIN_BOTTLES_DIR, False)]

    if args.architect:
        # pylint: disable-next=import-outside-toplevel
        from ...suggest.architect import architect_suggest
        proposal = architect_suggest(args.task, cwd)
    else:
        # pylint: disable-next=import-outside-toplevel
        from ...suggest.rules import rules_suggest
        proposal = rules_suggest(args.task, cwd)

    validated, _notes = validate_proposal(proposal, dirs)

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
    # pylint: disable=import-outside-toplevel
    from ...contract.yaml_subset import serialize_yaml_subset
    from ...manifest.loader import BUILTIN_BOTTLES_DIR
    from ...manifest.schema import is_valid_entity_name
    from ...manifest.versions import record_or_warn
    from ...paths import bot_bottle_root
    from ...suggest.validate import proposal_to_frontmatter

    if not is_valid_entity_name(name):
        sys.stderr.write(
            f"{PROG} bottles suggest --write: {name!r} is not a valid bottle name "
            f"(lowercase letters, digits, hyphens; must start with a letter)\n"
        )
        return 2

    root = bot_bottle_root()
    path = root / "bottles" / f"{name}.md"
    if path.exists():
        sys.stderr.write(f"{PROG} bottles suggest --write: {path} already exists\n")
        return 2

    fm = proposal_to_frontmatter(proposal)
    about_lines: list[str] = []
    if proposal.new_bottle and proposal.new_bottle.about:
        about_lines = [proposal.new_bottle.about]
    else:
        about_lines = [f"Bottle for: {', '.join(proposal.bottles) or 'basic'}"]
    if proposal.notes:
        about_lines.append("")
        about_lines.extend(f"Note: {n}" for n in proposal.notes)

    body = "\n".join(about_lines) + "\n"
    text = f"---\n{serialize_yaml_subset(fm)}---\n\n{body}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    record_or_warn(f"suggest: write bottles/{name}", root=root)
    print(f"wrote {path}", file=sys.stderr)
    return 0


# Type alias used only in _write_suggested_bottle's type hints.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ...suggest.proposal import Proposal


__all__ = ["cmd_bottles"]
