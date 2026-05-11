"""info: print env, skills, and prompt details for a named agent."""

from __future__ import annotations

import argparse

from ..log import info
from ..manifest import Manifest
from ._common import PROG, USER_CWD


def cmd_info(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} info", add_help=True)
    parser.add_argument("name", help="agent name defined in claude-bottle.json")
    args = parser.parse_args(argv)

    manifest = Manifest.resolve(USER_CWD)
    manifest.require_agent(args.name)

    agent = manifest.agents[args.name]
    bottle = manifest.bottle_for(args.name)
    env_names = list(bottle.env.keys())
    prompt_first_line = agent.prompt.splitlines()[0] if agent.prompt else ""

    print()
    info(f"agent           : {args.name}")
    info(f"env (names only): {', '.join(env_names) if env_names else '(none)'}")
    info(f"skills          : {' '.join(agent.skills) if agent.skills else '(none)'}")
    info(
        f"prompt          : {len(agent.prompt)} chars; "
        f"first line: {prompt_first_line or '(empty)'}"
    )
    info(f"bottle             : {agent.bottle}")
    if bottle.ssh:
        for e in bottle.ssh:
            info(
                f"  ssh host      : {e.Host} "
                f"(Hostname={e.Hostname}, User={e.User}, "
                f"Port={e.Port}, IdentityFile={e.IdentityFile})"
            )
            if e.KnownHostKey:
                info(f"    KnownHostKey: {e.KnownHostKey}")
    else:
        info("  ssh hosts     : (none)")
    print()
    return 0
