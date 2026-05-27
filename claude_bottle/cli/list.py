"""list: list available agents or active bottles."""

from __future__ import annotations

import argparse
import sys

from ..backend import enumerate_active_bottles
from ..manifest import Manifest
from ._common import PROG, USER_CWD


def cmd_list(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} list", add_help=True)
    parser.add_argument("scope", choices=["available", "active"])
    args = parser.parse_args(argv)

    if args.scope == "available":
        manifest = Manifest.resolve(USER_CWD)
        for name in manifest.agents.keys():
            print(name)
        return 0

    # `active` enumerates every backend (docker + smolmachines)
    # so smolmachines bottles aren't hidden behind the env var.
    active = enumerate_active_bottles()
    if not active:
        print("no active claude-bottle bottles", file=sys.stderr)
        return 0
    # One line per bottle: `<backend>\t<slug>\t<agent>\t<status>`.
    # Tab-separated keeps the format stable for shell pipelines;
    # the dashboard renders the same data through its own
    # formatter.
    for b in active:
        services = ",".join(b.services) if b.services else "-"
        print(f"{b.backend_name}\t{b.slug}\t{b.agent_name}\t{services}")
    return 0
