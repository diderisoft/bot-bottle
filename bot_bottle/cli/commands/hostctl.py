"""hostctl: start, stop or check the host controller, and drive the
orchestrator through it (PRD 0092)."""

from __future__ import annotations

import dataclasses
import sys

from ...contract.hostctl import ORCHESTRATOR_PATH
from ...control.hostctl import runner
from ...control.hostctl.client import HostControlClient
from ...control.hostctl.orchestrator import ensure_via_daemon
from ..constants import PROG
from ..settings import settings_from_env

_USAGE = (
    f"usage: {PROG} hostctl {{status|start|stop [--force]|jobs}}\n"
    f"       {PROG} hostctl orchestrator {{status|ensure|restart}}\n"
)
_DAEMON_ACTIONS = ("status", "start", "stop", "jobs")
_ORCHESTRATOR_ACTIONS = ("status", "ensure", "restart")


def _describe(state: runner.HostControlState) -> str:
    return f"host controller: running at {state.url} (pid {state.pid}, build {state.build_id})"


def _daemon(action: str, *, force: bool = False) -> int:
    if action == "start":
        print(_describe(runner.ensure_running()))
        return 0
    if action == "stop":
        stopped = runner.stop(force=force)
        print("host controller: stopped" if stopped else "host controller: not running")
        return 0
    state = runner.running()
    if state is None:
        print("host controller: not running")
        return 1
    if action == "status":
        print(_describe(state))
    else:
        for job in HostControlClient(state, runner.cli_token()).jobs():
            print(f"{job['id']}\t{job['kind']}\t{job['state']}")
    return 0


def _orchestrator(action: str) -> int:
    from ...backend import get_bottle_backend  # pylint: disable=import-outside-toplevel

    settings = settings_from_env()
    backend = get_bottle_backend(settings).name
    if action != "status":
        print(ensure_via_daemon(settings, backend, restart=action == "restart"))
        return 0
    request = dataclasses.replace(
        settings, operation=dataclasses.replace(settings.operation, backend=backend))
    client = HostControlClient(runner.ensure_running(), runner.cli_token())
    body = client.post(f"{ORCHESTRATOR_PATH}/status", {"settings": request.to_json()})
    running = bool(body.get("running"))
    where = f"running at {body.get('url')}" if running else "not running"
    print(f"{body.get('backend')} orchestrator: {where}")
    return 0 if running else 1


def cmd_hostctl(argv: list[str]) -> int:
    if argv in (["-h"], ["--help"]):
        sys.stderr.write(_USAGE)
        sys.stderr.write("\nThe per-host daemon behind bottle and orchestrator management.\n")
        return 0
    force = argv == ["stop", "--force"]
    daemon = (len(argv) == 1 and argv[0] in _DAEMON_ACTIONS) or force
    orch = len(argv) == 2 and argv[0] == "orchestrator" and argv[1] in _ORCHESTRATOR_ACTIONS
    if not (daemon or orch):
        sys.stderr.write(_USAGE)
        return 2
    try:
        return _daemon(argv[0], force=force) if daemon else _orchestrator(argv[1])
    except runner.HostControlError as e:
        sys.stderr.write(f"{PROG}: {e}\n")
        return 1
