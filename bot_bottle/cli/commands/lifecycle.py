"""The bot-bottle host service as one unit: `install`, `up`, `down`,
`status` and `jobs`, across the host controller and each backend's
infra pair.
"""

from __future__ import annotations

import argparse
import os
import sys

from ...backend import enumerate_active_agents, get_bottle_backend
from ...control.hostctl import runner, service_unit
from ...control.hostctl.client import HostControlClient
from ...control.hostctl.orchestrator import ensure_via_daemon
from ...control.hostctl.sessions import MIN_TMUX_VERSION, tmux_version
from ..constants import PROG
from ..launch_client import confirm_controller_restart
from ..settings import settings_from_env


def _parser(name: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{PROG} {name}", description=description)
    parser.add_argument("--backend", default=None,
                        help="backend to act on (default: $BOT_BOTTLE_BACKEND or the host default)")
    return parser


def _fail(message: str) -> int:
    sys.stderr.write(f"{PROG}: {message}\n")
    return 1


def cmd_up(argv: list[str]) -> int:
    parser = _parser("up", "Start the host controller and the backend's orchestrator and gateway.")
    parser.add_argument("--restart", action="store_true",
                        help="restart the host controller (running bottles keep running), "
                             "then the orchestrator and gateway")
    ns = parser.parse_args(argv)
    settings = settings_from_env()
    backend = get_bottle_backend(settings, ns.backend).name
    try:
        if ns.restart:
            runner.restart(confirm_restart=confirm_controller_restart)
        url = ensure_via_daemon(settings, backend, restart=ns.restart)
    except runner.HostControlError as e:
        return _fail(str(e))
    print(f"{backend} orchestrator: running at {url}")
    return 0


def cmd_down(argv: list[str]) -> int:
    parser = _parser("down", "Stop the orchestrator, gateway and host controller.")
    parser.add_argument("--force", action="store_true",
                        help="stop even while bottles are running (they're orphaned)")
    ns = parser.parse_args(argv)
    settings = settings_from_env()
    try:
        state = runner.running()
        held = runner.held_bottles(state) if state else []
        if held and not ns.force:
            return _fail(f"{len(held)} bottle(s) are running ({', '.join(held)}); "
                         f"tear them down first (`{PROG} cleanup`) or use `{PROG} down --force`")
        backend = get_bottle_backend(settings, ns.backend)
        backend.infra_service().stop()
        print(f"{backend.name} orchestrator and gateway: stopped")
        stopped = runner.stop(force=ns.force)
    except runner.HostControlError as e:
        return _fail(str(e))
    print("host controller: stopped" if stopped else "host controller: not running")
    return 0


def cmd_status(argv: list[str]) -> int:
    ns = _parser("status", "Show the host controller, orchestrator and bottles.").parse_args(argv)
    settings = settings_from_env()
    state = runner.running()
    print("host controller: " + (f"running at {state.url} (pid {state.pid}, build {state.build_id})"
                                 if state else "not running"))
    print("autostart: " + ("installed" if service_unit.installed() else "not installed"))
    backend = get_bottle_backend(settings, ns.backend)
    service = backend.infra_service()
    healthy = service.is_healthy()
    where = f"running at {service.url()}" if healthy else "not running"
    print(f"{backend.name} orchestrator: {where}")
    bottles = enumerate_active_agents(settings)
    print(f"bottles: {len(bottles)} running" + (f" (see `{PROG} active`)" if bottles else ""))
    return 0 if state and healthy else 1


def cmd_jobs(argv: list[str]) -> int:
    if argv:
        sys.stderr.write(f"usage: {PROG} jobs\n\nThe host controller's recent jobs.\n")
        return 0 if argv in (["-h"], ["--help"]) else 2
    state = runner.running()
    if state is None:
        print("host controller: not running")
        return 1
    # pylint: disable-next=import-outside-toplevel
    from ..table import print_table
    jobs = HostControlClient(state, runner.cli_token()).jobs()
    if not jobs:
        print("no recent jobs", file=sys.stderr)
        return 0
    print_table(("ID", "KIND", "STATE"),
                [(str(j["id"]), str(j["kind"]), str(j["state"])) for j in jobs])
    return 0


def cmd_install(argv: list[str]) -> int:
    parser = _parser("install", "Set up this host: backend prerequisites, tmux, autostart.")
    parser.add_argument("--no-autostart", action="store_true",
                        help="don't install the service that keeps the host controller running")
    ns = parser.parse_args(argv)
    version = tmux_version()
    if version is None or version < MIN_TMUX_VERSION:
        found = "not installed" if version is None else f"{version[0]}.{version[1]}"
        return _fail(f"tmux {MIN_TMUX_VERSION[0]}.{MIN_TMUX_VERSION[1]}+ is required ({found}); "
                     "install it with brew, apt or nix, then rerun")
    code = get_bottle_backend(settings_from_env(), ns.backend).setup()
    if code != 0:
        return code
    if ns.no_autostart:
        return 0
    try:
        runner.stop()  # a CLI-started daemon would run beside the service's
        spec = service_unit.ServiceSpec(python=sys.executable, path_env=os.environ.get("PATH", ""))
        print(f"autostart: installed ({service_unit.install(spec)})")
        state = runner.ensure_running()
    except runner.HostControlError as e:
        return _fail(str(e))
    print(f"host controller: running at {state.url}")
    return 0


__all__ = ["cmd_down", "cmd_install", "cmd_jobs", "cmd_status", "cmd_up"]
