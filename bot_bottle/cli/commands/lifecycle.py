"""The bot-bottle host service as one unit: `install`, `up`, `down`,
`status` and `jobs`, across the host controller and each backend's
infra pair.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys

from ...backend import enumerate_active_agents, get_bottle_backend
from ...control.hostctl import runner, service_unit
from ...control.hostctl.build import RESTART_NEEDED
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


def _yes(held: list[str]) -> bool:
    del held
    return True


def cmd_up(argv: list[str]) -> int:
    parser = _parser("up", "Start the host controller and the backend's orchestrator and gateway.")
    parser.add_argument("--restart", action="store_true",
                        help="restart the host controller (running bottles keep running); "
                             "the orchestrator and gateway are rebooted only if their images "
                             "or settings changed")
    parser.add_argument("--restart-infra", action="store_true",
                        help="also reboot the orchestrator and gateway even if they're "
                             "current (running bottles are reattached; their network drops "
                             "for the seconds it takes)")
    parser.add_argument("--build", action="store_true",
                        help="build the orchestrator and gateway images from this install's "
                             "source instead of pulling a release's (for running from main; "
                             "same as BOT_BOTTLE_INFRA_BUILD=local). Later launches keep "
                             "building until an `up` without it; one that finds a current "
                             "local build running, with no release to pull, keeps it")
    parser.add_argument("--allow-reattach-failures", action="store_true",
                        help="if a running bottle can't be reattached to the restarted "
                             "gateway, name it and bring the rest up instead of failing "
                             "(then stop and resume it)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="with --restart, don't ask before moving running bottles to "
                             "the new host controller (for scripts and services)")
    ns = parser.parse_args(argv)
    settings = settings_from_env()
    if ns.build:
        settings = dataclasses.replace(
            settings, infra=dataclasses.replace(settings.infra, infra_build_local=True))
    backend = get_bottle_backend(settings, ns.backend).name
    try:
        if ns.restart:
            runner.restart(confirm_restart=_yes if ns.yes else confirm_controller_restart)
        # A current pair is adopted: rebooting it would only cut running
        # bottles off for nothing. New images or settings reboot it anyway.
        url = ensure_via_daemon(settings, backend, restart=ns.restart_infra,
                                allow_reattach_failures=ns.allow_reattach_failures)
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
                         f"tear them down first (`{PROG} cleanup bots`) "
                         f"or use `{PROG} down --force`")
        backend = get_bottle_backend(settings, ns.backend)
        backend.infra_service().stop()
        print(f"{backend.name} orchestrator and gateway: stopped")
        stopped = runner.stop(force=ns.force)
    except runner.HostControlError as e:
        return _fail(str(e))
    print("host controller: stopped" if stopped else "host controller: not running")
    return 0


def cmd_status(argv: list[str]) -> int:
    parser = _parser("status", "Show the host controller, orchestrator, console link and "
                               "bottled agents.")
    parser.add_argument("--jobs", action="store_true", help="also list the host controller's "
                                                             "recent jobs")
    ns = parser.parse_args(argv)
    settings = settings_from_env()
    state = runner.running()
    # pylint: disable-next=import-outside-toplevel
    from .doctor import environment_line
    print(f"environment: {environment_line()}")
    print("host controller: " + (f"running at {state.url} (pid {state.pid}, build {state.build_id})"
                                 if state else "not running"))
    if state and runner.restart_needed(state):
        print(f"host controller: {RESTART_NEEDED}")
    print("autostart: " + ("installed" if service_unit.installed() else "not installed"))
    # pylint: disable-next=import-outside-toplevel
    from .remote import link_line
    print(link_line())
    # The environment's orchestrator: whichever backend's its bring-up recorded.
    # pylint: disable-next=import-outside-toplevel
    from .doctor import orchestrator_report
    report = orchestrator_report()
    healthy = report.healthy
    if report.recorded is None:
        print(f"orchestrator: {report.headline()}")
    else:
        where = (f"running at {report.recorded.url}" if healthy
                 else f"not answering at {report.recorded.url}; run `{PROG} up`")
        print(f"{report.recorded.backend} orchestrator: {where}")
    for warning in report.warnings():
        print(f"warning: {warning}")
    print(_definitions_line())
    bottles = enumerate_active_agents(settings)
    print(f"bottled agents: {len(bottles)} running" + (f" (see `{PROG} bots`)" if bottles else ""))
    if ns.jobs and state:
        print()
        _print_jobs(state)
    return 0 if state and healthy else 1


def _definitions_line() -> str:
    """How many agent and bottle definitions this directory sees."""
    # pylint: disable-next=import-outside-toplevel
    from ...manifest import ManifestError, ManifestIndex
    try:
        index = ManifestIndex.resolve(os.getcwd(), missing_ok=True)
        agents = len(index.all_agent_names)
        bottles = len(index.all_bottle_names) if index.home_md is not None else 0
    except (ManifestError, OSError) as e:
        return f"definitions: can't read them ({e})"
    return (f"definitions: {agents} agents, {bottles} bottles "
            f"(`{PROG} agents`, `{PROG} bottles`)")


def cmd_jobs(argv: list[str]) -> int:
    """The old name for `status --jobs`'s job list, kept unlisted."""
    if argv:
        sys.stderr.write(f"usage: {PROG} status --jobs\n\nThe host controller's recent jobs.\n")
        return 0 if argv in (["-h"], ["--help"]) else 2
    state = runner.running()
    if state is None:
        print("host controller: not running")
        return 1
    _print_jobs(state)
    return 0


def _print_jobs(state: runner.HostControlState) -> None:
    # pylint: disable-next=import-outside-toplevel
    from ..table import print_table
    jobs = HostControlClient(state, runner.cli_token()).jobs()
    if not jobs:
        print("no recent jobs", file=sys.stderr)
        return
    print_table(("ID", "KIND", "STATE"),
                [(str(j["id"]), str(j["kind"]), str(j["state"])) for j in jobs])


def cmd_install(argv: list[str]) -> int:  # pylint: disable=too-many-return-statements
    parser = _parser("install", "Set up this host: backend prerequisites, tmux, autostart. "
                                "Run the privileged part with sudo where the backend prints it "
                                "(e.g. `sudo bot-bottle install --backend=macos-container`).")
    parser.add_argument("--no-autostart", action="store_true",
                        help="don't install the service that keeps the host controller running")
    parser.add_argument("--undo", action="store_true",
                        help="undo the backend's host setup (its firewall or network pool) "
                             "instead; `uninstall` removes everything")
    ns = parser.parse_args(argv)
    backend = get_bottle_backend(settings_from_env(), ns.backend)
    if ns.undo:
        return backend.teardown()
    if os.geteuid() == 0:
        # Under sudo, only the backend's privileged pieces: tmux and autostart
        # are the user's own account's business.
        return backend.setup()
    version = tmux_version()
    if version is None or version < MIN_TMUX_VERSION:
        found = "not installed" if version is None else f"{version[0]}.{version[1]}"
        return _fail(f"tmux {MIN_TMUX_VERSION[0]}.{MIN_TMUX_VERSION[1]}+ is required ({found}); "
                     "install it with brew, apt or nix, then rerun")
    code = backend.setup()
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
