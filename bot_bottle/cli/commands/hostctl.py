"""hostctl: start, stop or check the host controller (PRD 0092)."""

from __future__ import annotations

import sys

from ...control.hostctl import runner
from ..constants import PROG

_USAGE = f"usage: {PROG} hostctl {{status|start|stop}}\n"


def _describe(state: runner.HostControlState) -> str:
    return f"host controller: running at {state.url} (pid {state.pid}, build {state.build_id})"


def _run(action: str) -> int:
    if action == "status":
        state = runner.running()
        print(_describe(state) if state else "host controller: not running")
        return 0 if state else 1
    if action == "start":
        print(_describe(runner.ensure_running()))
        return 0
    print("host controller: stopped" if runner.stop() else "host controller: not running")
    return 0


def cmd_hostctl(argv: list[str]) -> int:
    if argv in (["-h"], ["--help"]):
        sys.stderr.write(_USAGE)
        sys.stderr.write("\nThe per-host daemon behind bottle and orchestrator management.\n")
        return 0
    if len(argv) != 1 or argv[0] not in ("status", "start", "stop"):
        sys.stderr.write(_USAGE)
        return 2
    try:
        return _run(argv[0])
    except runner.HostControlError as e:
        sys.stderr.write(f"{PROG}: {e}\n")
        return 1
