"""list: list available agents or active containers."""

from __future__ import annotations

import argparse
import subprocess

from .. import docker as docker_mod
from ..log import info
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

    docker_mod.require_docker()
    result = subprocess.run(
        [
            "docker", "ps",
            "--filter", "name=^claude-bottle-",
            "--format", "{{.Names}}\t{{.Status}}",
        ],
        capture_output=True,
        text=True,
    )
    containers = (result.stdout or "").strip()
    if not containers:
        info("no active claude-bottle containers")
        return 0
    print()
    for line in containers.splitlines():
        name, _, status = line.partition("\t")
        info(f"container: {name}  status: {status}")
    print()
    return 0
