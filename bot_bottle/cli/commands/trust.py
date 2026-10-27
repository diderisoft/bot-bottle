"""bot-bottle trust: review and trust a project's bottles and agents on this host.

  bot-bottle trust <name>       show what the bottle (`./<name>`) grants beyond `basic` (or
                                what the agent gets from this host) and trust it
                                (asks first; --yes skips)
  bot-bottle trust show <name>
                                show what it grants, without trusting it
  bot-bottle trust --list       the bottles and agents this host trusts

Project bottles (`./.bot-bottle/bottles/`), and project agents
(`./.bot-bottle/agents/`) whose provider grants something or that run on one
of your bottles, run only once their exact contents are trusted; any change
asks again. A project bottle is named `./<name>` (`./main`); a bare name is a
project agent. Run in the project's directory.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from ...lib.log import die, info
from pathlib import Path

from ...manifest import ManifestIndex, trust
from ...manifest.agent import ManifestAgentProvider
from ...contract.yaml_subset import parse_frontmatter
from ...manifest.loader import PROJECT_PREFIX, load_bottle_chain, scan_agent_names
from ...util import read_tty_line
from ..constants import PROG


def _project_agent(
    index: ManifestIndex, name: str,
) -> tuple[Path, ManifestAgentProvider, dict[str, Any]] | None:
    """The project agent `name`: its path, provider, and the `check_agent`
    arguments for what it gets from the host (its own `bottle:`). None for a
    bottle (`./<name>`)."""
    if index.cwd_md is None or name.startswith(PROJECT_PREFIX):
        return None
    path = index.cwd_md / "agents" / f"{name}.md"
    if not path.is_file():
        return None
    fm, _ = parse_frontmatter(path.read_text())
    provider = ManifestAgentProvider.from_dict(name, fm.get("agent_provider", {}))
    context: dict[str, Any] = {}
    if index.home_md is not None:
        context["home_agent"] = scan_agent_names(index.home_md / "agents").get(name)
    bottle_name = str(fm.get("bottle") or "")
    if bottle_name:
        bottle, sources = load_bottle_chain(bottle_name, index.bottle_dirs())
        context.update(bottle_name=bottle_name, bottle=bottle, sources=sources)
    return path, provider, context


def _review(name: str) -> trust.UntrustedBottleError | None:
    """The bottle's (or project agent's) trust question, or None when it
    needs no trust."""
    index = ManifestIndex.resolve(os.getcwd())
    agent = _project_agent(index, name)
    if agent is not None:
        path, provider, context = agent
        try:
            trust.check_agent(name, path, provider, **context)
        except trust.UntrustedBottleError as e:
            return e
        info(f"{name} is trusted on this host (or gets nothing from it)")
        return None
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
            print("no project bottles or agents are trusted on this host")
        for e in entries:
            print(f"{e.bottle:<20} {e.path}  (trusted {e.trusted_at}, {e.hash[:12]})")
        return 0
    if argv[0] == "show":
        if len(argv) < 2:
            die(f"usage: {PROG} trust show <name>")
        index = ManifestIndex.resolve(os.getcwd())
        agent = _project_agent(index, argv[1])
        if agent is not None:
            _, provider, context = agent
            granted = (trust.provider_grants(provider) + trust.host_context(argv[1], **context)
                       or ["nothing (it gets nothing from this host)"])
        else:
            bottle, _ = load_bottle_chain(argv[1], index.bottle_dirs())
            granted = trust.beyond_basic(trust.grants(bottle)) or ["nothing beyond basic"]
        for line in granted:
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
