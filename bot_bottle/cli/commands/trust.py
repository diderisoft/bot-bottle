"""bot-bottle trust — review and trust a project's bottles on this host.

  bot-bottle trust <bottle>     show what the project bottle grants beyond
                                `basic` and trust it (asks first; --yes skips)
  bot-bottle trust show <bottle>
                                show what it grants, without trusting it
  bot-bottle trust --list       the bottles this host trusts

A project can ship bottles in `./.bot-bottle/bottles/`. They run only once
this host has trusted their exact contents; any change needs trusting
again. Your own bottles in ~/.bot-bottle/ and the built-in `basic` need no
trust. Run it in the project's directory.
"""

from __future__ import annotations

import os
import sys

from ...lib.log import die, info
from ...manifest import ManifestIndex, trust
from ...manifest.loader import load_bottle_chain
from ...util import read_tty_line
from ..constants import PROG


def _review(name: str) -> trust.UntrustedBottleError | None:
    """The bottle's trust question, or None when it needs no trust."""
    index = ManifestIndex.resolve(os.getcwd())
    bottle, sources = load_bottle_chain(name, index.bottle_dirs())
    if not any(s.project for s in sources):
        info(f"{name} isn't a project bottle; it needs no trust")
        return None
    try:
        trust.check(name, bottle, sources)
    except trust.UntrustedBottleError as e:
        return e
    info(f"{name} is trusted on this host (or grants nothing beyond basic)")
    return None


def cmd_trust(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        sys.stderr.write(__doc__ or "")
        return 0 if argv else 2
    if argv[0] == "--list":
        entries = trust.load_trusted()
        if not entries:
            print("no project bottles are trusted on this host")
        for e in entries:
            print(f"{e.bottle:<20} {e.path}  (trusted {e.trusted_at}, {e.hash[:12]})")
        return 0
    if argv[0] == "show":
        if len(argv) < 2:
            die(f"usage: {PROG} trust show <bottle>")
        index = ManifestIndex.resolve(os.getcwd())
        bottle, _ = load_bottle_chain(argv[1], index.bottle_dirs())
        for line in trust.beyond_basic(trust.grants(bottle)) or ["nothing beyond basic"]:
            print(line)
        return 0
    name = argv[0]
    question = _review(name)
    if question is None:
        return 0
    if "--yes" not in argv:
        sys.stderr.write(trust.prompt_text(question))
        sys.stderr.flush()
        if read_tty_line() not in ("y", "Y", "yes", "YES"):
            info("not trusted")
            return 1
    trust.trust(question)
    info(f"trusted {name} on this host")
    return 0
