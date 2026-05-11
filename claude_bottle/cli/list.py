"""list: list available agents or active bottles."""

from __future__ import annotations

import argparse

from ..platform import get_bottle_platform
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

    get_bottle_platform().list_active()
    return 0
