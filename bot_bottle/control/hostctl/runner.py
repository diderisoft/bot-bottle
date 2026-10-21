"""Start, find and stop the host controller from the CLI.

The daemon publishes `{pid, port, build_id}` in a state file under
`bot_bottle_root()/hostctl`. A CLI trusts that file only after `GET /status`
on the port answers with the same pid. `ensure_running` holds a lock so
concurrent CLIs start one daemon, and restarts a daemon running other code.
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
from collections.abc import Generator
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


def ensure_running(timeout: float = START_TIMEOUT_SECONDS) -> HostControlState:
    """Return the live host controller, starting it (or replacing one that
    runs different code) first if needed. One that holds bottles is kept:
    replacing it would orphan them."""
    with _lock():
        state = running()
        if state is not None and state.build_id == current_build_id():
            return state
        if state is not None:
            held = held_bottles(state)
            if held:
                warn(f"host controller runs different code but holds {len(held)} bottle(s) "
                     f"({', '.join(held)}); using it until they're done")
                return state
            info("host controller runs different code; restarting it")
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
    """Stop the live host controller. False if none was running. Refuses
    while it holds bottles, which would be orphaned, unless `force`."""
    with _lock():
        state = running()
        if state is None:
            return False
        held = held_bottles(state)
        if held and not force:
            raise HostControlError(
                f"the host controller holds {len(held)} bottle(s) ({', '.join(held)}); "
                "stopping it would orphan them. Tear them down first (`cleanup`) "
                "or stop it anyway with `hostctl stop --force`")
        _stop(state)
        return True


def _start(timeout: float) -> HostControlState:
    from . import service_unit  # pylint: disable=import-outside-toplevel

    if service_unit.installed():
        # The service manager owns the daemon; ask it, then wait as usual.
        service_unit.start()
        return _await_state(None, timeout)
    directory = state_dir()
    log_path = directory / "hostctl.log"
    env = dict(os.environ)
    # The daemon must import the same package this CLI runs, checkout or install.
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(PACKAGE_ROOT.parent), env.get("PYTHONPATH", "")) if p
    )
    # Detached in its own session, so it outlives this CLI and its terminal.
    pid = os.posix_spawn(
        sys.executable, [sys.executable, "-m", "bot_bottle.control.hostctl"], env,
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
    """Wait for a live daemon to publish its state: the one with `pid`, or
    (under a service manager) any. Raises if `pid` exits or time runs out."""
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
            # Reap it if this process started it; otherwise it isn't our child.
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
    "HostControlError", "HostControlState", "clear_state", "cli_token", "ensure_running",
    "probe", "read_state", "running", "state_dir", "stop", "write_state",
]
