"""cleanup: stop and remove all active claude-bottle containers."""

from __future__ import annotations

import subprocess
import sys

from .. import docker as docker_mod
from ..log import info
from ._common import read_tty_line


def cmd_cleanup(_argv: list[str]) -> int:
    docker_mod.require_docker()
    result = subprocess.run(
        ["docker", "ps", "--filter", "name=^claude-bottle-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    containers = (result.stdout or "").strip()
    if not containers:
        info("no active claude-bottle containers")
        return 0
    print(file=sys.stderr)
    for name in containers.splitlines():
        info(f"found: {name}")
    print(file=sys.stderr)
    sys.stderr.write("claude-bottle: remove all of the above? [y/N] ")
    sys.stderr.flush()
    reply = read_tty_line()
    if reply not in ("y", "Y", "yes", "YES"):
        info("aborted")
        return 0
    for name in containers.splitlines():
        info(f"removing {name}")
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    info("done")
    return 0
