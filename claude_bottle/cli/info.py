"""info: print env, skills, and prompt details for a named agent."""

from __future__ import annotations

import argparse

from ..log import info
from ..manifest import (
    manifest_agent_bottle,
    manifest_env_names,
    manifest_prompt,
    manifest_require_agent,
    manifest_resolve,
    manifest_skills,
    manifest_ssh,
)
from ._common import PROG, USER_CWD


def cmd_info(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} info", add_help=True)
    parser.add_argument("name", help="agent name defined in claude-bottle.json")
    args = parser.parse_args(argv)

    manifest = manifest_resolve(USER_CWD)
    manifest_require_agent(manifest, args.name)

    env_names = manifest_env_names(manifest, args.name)
    skill_names = manifest_skills(manifest, args.name)
    prompt_content = manifest_prompt(manifest, args.name)
    prompt_first_line = prompt_content.splitlines()[0] if prompt_content else ""

    bottle_name = manifest_agent_bottle(manifest, args.name)
    ssh_entries = manifest_ssh(manifest, args.name)

    print()
    info(f"agent           : {args.name}")
    info(f"env (names only): {', '.join(env_names) if env_names else '(none)'}")
    info(f"skills          : {' '.join(skill_names) if skill_names else '(none)'}")
    info(
        f"prompt          : {len(prompt_content)} chars; "
        f"first line: {prompt_first_line or '(empty)'}"
    )
    if bottle_name:
        info(f"bottle             : {bottle_name}")
        if ssh_entries:
            for e in ssh_entries:
                info(
                    f"  ssh host      : {e.get('Host')} "
                    f"(Hostname={e.get('Hostname')}, User={e.get('User')}, "
                    f"Port={e.get('Port')}, IdentityFile={e.get('IdentityFile')})"
                )
                if e.get("KnownHostKey"):
                    info(f"    KnownHostKey: {e['KnownHostKey']}")
        else:
            info("  ssh hosts     : (none)")
    else:
        info("bottle             : (none)")
    print()
    return 0
