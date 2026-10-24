"""Start, find and stop the host controller from the CLI. The daemon publishes
`{pid, port, build_id}` under `bot_bottle_root()/hostctl`, trusted only once
`GET /status` reports the same pid. `ensure_running` locks so concurrent CLIs
start one daemon, and replaces one running other code.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ...contract.auth import ROLE_CLI
from ...contract.hostctl import BOTTLES_PATH, HOST_CONTROL, SERVICE_NAME, STATUS_PATH, bearer
from ...lib.log import info, warn
from ...paths import bot_bottle_root
from ...trust_domain import mint
from .build import PACKAGE_ROOT, current_build_id

STATE_FILENAME = "state.json"
START_TIMEOUT_SECONDS = 15.0
STOP_TIMEOUT_SECONDS = 10.0
_POLL_SECONDS = 0.1


class HostControlError(RuntimeError):
    """The host controller couldn't be started, reached or stopped."""


class StaleHostControllerError(HostControlError):
    """The host controller runs older code than is on disk and holds bottles,
    and restarting it wasn't confirmed."""


# Asked before restarting a stale host controller that holds bottles: the ids,
# and whether to go ahead.
ConfirmRestart = Callable[[list[str]], bool]


def stale_message(held: list[str]) -> str:
    """Why a launch is refused while an outdated controller holds bottles."""
    return (f"bot-bottle was updated while {len(held)} bottle(s) are running "
            f"({', '.join(held)}); restart the host controller to launch: "
            "`bot-bottle up --restart`. The agents keep running")


@dataclass(frozen=True)
class HostControlState:
    pid: int
    port: int
    build_id: str

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def state_dir() -> Path:
    return bot_bottle_root() / "hostctl"


def write_state(state: HostControlState) -> None:
    """Publish `state` atomically (daemon side)."""
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f".{STATE_FILENAME}.{state.pid}"
    tmp.write_text(json.dumps({"pid": state.pid, "port": state.port, "build_id": state.build_id}))
    os.replace(tmp, directory / STATE_FILENAME)


def clear_state(pid: int) -> None:
    """Remove the state file if it still names `pid` (daemon side, on exit)."""
    current = read_state()
    if current is not None and current.pid == pid:
        (state_dir() / STATE_FILENAME).unlink(missing_ok=True)


def read_state() -> HostControlState | None:
    try:
        raw = json.loads((state_dir() / STATE_FILENAME).read_text())
        return HostControlState(
            pid=int(raw["pid"]), port=int(raw["port"]), build_id=str(raw["build_id"])
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def probe(state: HostControlState, timeout: float = 2.0) -> bool:
    """True if a host controller with `state.pid` answers on `state.port`."""
    try:
        with urllib.request.urlopen(state.url + STATUS_PATH, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except (OSError, ValueError, urllib.error.URLError):
        return False
    return body.get("service") == SERVICE_NAME and body.get("pid") == state.pid


def running() -> HostControlState | None:
    """The live host controller's state, or None."""
    state = read_state()
    return state if state is not None and probe(state) else None


@contextmanager
def _lock() -> Generator[None]:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "lock", "w", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def ensure_running(
    timeout: float = START_TIMEOUT_SECONDS, *, confirm_restart: ConfirmRestart | None = None,
) -> HostControlState:
    """The live host controller, starting it (or replacing one running other
    code) if needed. A replaced controller's bottles keep running and the new
    one adopts them. With bottles held, `confirm_restart` decides: yes
    restarts, no raises `StaleHostControllerError`. Without it the old
    controller is kept, for callers that launch nothing."""
    with _lock():
        state = running()
        if state is not None and state.build_id == current_build_id():
            return state
        if state is not None:
            held = held_bottles(state)
            if held and confirm_restart is None:
                warn(f"host controller runs different code but holds {len(held)} bottle(s) "
                     f"({', '.join(held)}); using it until they're done")
                return state
            if held and confirm_restart is not None and not confirm_restart(held):
                raise StaleHostControllerError(stale_message(held))
            info("host controller runs different code; restarting it")
            _stop(state)
        return _start(timeout)


def restart(
    timeout: float = START_TIMEOUT_SECONDS, *, confirm_restart: ConfirmRestart,
) -> HostControlState:
    """Restart the host controller whatever code it runs; its bottles keep
    running and the new one adopts them. With bottles held, `confirm_restart`
    must agree or `StaleHostControllerError` is raised."""
    with _lock():
        state = running()
        if state is not None:
            held = held_bottles(state)
            if held and not confirm_restart(held):
                raise StaleHostControllerError(
                    f"not restarting; {len(held)} bottle(s) are running ({', '.join(held)})")
            info("restarting the host controller")
            _stop(state)
        return _start(timeout)


def held_bottles(state: HostControlState) -> list[str]:
    """Ids of the bottles the daemon at `state` holds open; [] if it can't say."""
    req = urllib.request.Request(state.url + BOTTLES_PATH, headers=bearer(cli_token()))
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
    except (OSError, ValueError, urllib.error.URLError):
        return []
    bottles = body.get("bottles") if isinstance(body, dict) else None
    return [str(b.get("id")) for b in bottles or [] if isinstance(b, dict) and b.get("held")]


def cli_token() -> str:
    """A `cli` token for the host controller, signed with its host key."""
    return mint(HOST_CONTROL, ROLE_CLI)


def stop(*, force: bool = False) -> bool:
    """Stop the host controller; False if none was running. Refuses while it
    holds bottles unless `force`."""
    with _lock():
        state = running()
        if state is None:
            return False
        held = held_bottles(state)
        if held and not force:
            raise HostControlError(
                f"the host controller holds {len(held)} bottle(s) ({', '.join(held)}); "
                "stopping it would orphan them. Tear them down first (`cleanup`) "
                "or stop it anyway with `down --force`")
        _stop(state)
        return True


def leave_callers_directory() -> None:
    """Move the daemon out of the directory `bot-bottle` was run in, into its
    state directory: tmux, and so every session pane, starts in the daemon's
    (#706)."""
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chdir(directory)


def _start(timeout: float) -> HostControlState:
    from . import service_unit  # pylint: disable=import-outside-toplevel

    if service_unit.installed():
        # The service manager owns the daemon.
        service_unit.start()
        return _await_state(None, timeout)
    directory = state_dir()
    log_path = directory / "hostctl.log"
    env = dict(os.environ)
    # Import the same package this CLI runs.
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(PACKAGE_ROOT.parent), env.get("PYTHONPATH", "")) if p
    )
    # Detached in its own session, to outlive this CLI. `-P`: `-m` would put
    # the caller's directory first on sys.path, so a repo with a `bot_bottle/`
    # in it would run its own code on the host (#706).
    pid = os.posix_spawn(
        sys.executable, [sys.executable, "-P", "-m", "bot_bottle.control.hostctl"], env,
        file_actions=[
            (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
            (os.POSIX_SPAWN_OPEN, 1, str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600),
            (os.POSIX_SPAWN_DUP2, 1, 2),
        ],
        setsid=True,
    )
    try:
        return _await_state(pid, timeout)
    except HostControlError:
        os.kill(pid, signal.SIGTERM)
        raise


def _await_state(pid: int | None, timeout: float) -> HostControlState:
    """Wait for a daemon to publish its state: the one with `pid`, or any under
    a service manager. Raises if `pid` exits or time runs out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = read_state()
        if state is not None and (pid is None or state.pid == pid) and probe(state):
            return state
        if pid is not None:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                raise HostControlError(
                    f"host controller exited with {os.waitstatus_to_exitcode(status)}; "
                    f"see {state_dir() / 'hostctl.log'}")
        time.sleep(_POLL_SECONDS)
    raise HostControlError(f"host controller didn't answer within {timeout:.0f}s")


def _stop(state: HostControlState) -> None:
    try:
        os.kill(state.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            # Reap it if we started it.
            if os.waitpid(state.pid, os.WNOHANG)[0] == state.pid:
                return
        except ChildProcessError:
            pass
        try:
            os.kill(state.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(_POLL_SECONDS)
    raise HostControlError(f"host controller (pid {state.pid}) didn't stop")


__all__ = [
    "ConfirmRestart", "HostControlError", "HostControlState", "StaleHostControllerError",
    "restart", "stale_message", "clear_state", "cli_token", "ensure_running",
    "probe", "read_state", "running", "state_dir", "stop", "write_state",
]
