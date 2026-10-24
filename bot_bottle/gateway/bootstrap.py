"""Gateway supervisor: PID 1 in the gateway image.

Starts the daemons (egress, git-http, supervise), forwards SIGTERM/SIGINT,
prefixes each one's output with `[name] `, and restarts a daemon that dies. It
exits only on SIGTERM/SIGINT. `BOT_BOTTLE_GATEWAY_DAEMONS` selects a subset
(default: all). Stdlib only.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import IO, Sequence


# Under compose's 10 s `stop_grace_period`: SIGKILL anything still running this
# long after SIGTERM.
_GRACE_SECONDS = 8.0

# Main loop poll interval.
_POLL_INTERVAL = 0.1


@dataclass(frozen=True)
class _DaemonSpec:
    name: str
    argv: Sequence[str]


# Egress-only credential env prefixes; no other daemon sees them.
_EGRESS_ONLY_ENV_PREFIXES: tuple[str, ...] = ("EGRESS_TOKEN_",)
_READY_GATED_DAEMONS: tuple[str, ...] = ("git-http",)

# The orchestrator's signing key, stripped from every daemon's env in case it
# ever leaks into the gateway. Same value as
# `contract.identity.ORCHESTRATOR_TOKEN_ENV`, hardcoded to stay import-light.
_SIGNING_KEY_ENV = "BOT_BOTTLE_ORCHESTRATOR_TOKEN"


def _env_for_daemon(name: str, base_env: dict[str, str]) -> dict[str, str]:
    """A fresh per-daemon env: `EGRESS_TOKEN_*` for egress only, and the
    signing key for none."""
    env = dict(base_env)
    if name != "egress":
        env = {
            k: v for k, v in env.items()
            if not any(k.startswith(p) for p in _EGRESS_ONLY_ENV_PREFIXES)
        }
    env.pop(_SIGNING_KEY_ENV, None)
    return env


_DAEMONS: tuple[_DaemonSpec, ...] = (
    _DaemonSpec("egress", ("/bin/sh", "/app/egress-entrypoint.sh")),
    _DaemonSpec("git-http", ("python3", "-m", "bot_bottle.gateway.git_gate.http_backend")),
    _DaemonSpec("supervise", ("python3", "-m", "bot_bottle.gateway.supervisor.server")),
)


def _argv_for_daemon(name: str, argv: Sequence[str], env: dict[str, str]) -> list[str]:
    ready_file = env.get("BOT_BOTTLE_GIT_GATE_READY_FILE", "").strip()
    if name not in _READY_GATED_DAEMONS or not ready_file:
        return list(argv)
    return [
        "/bin/sh",
        "-c",
        "while [ ! -f \"$BOT_BOTTLE_GIT_GATE_READY_FILE\" ]; do "
        "sleep 0.1; "
        "done; "
        "exec \"$@\"",
        name,
        *argv,
    ]


def _selected_daemons(
    env: dict[str, str],
    all_daemons: Sequence[_DaemonSpec] | None = None,
) -> tuple[_DaemonSpec, ...]:
    """The daemons `BOT_BOTTLE_GATEWAY_DAEMONS` selects; all when unset.
    Unknown names are ignored. `all_daemons` defaults to `_DAEMONS` at call
    time."""
    if all_daemons is None:
        all_daemons = _DAEMONS
    raw = env.get("BOT_BOTTLE_GATEWAY_DAEMONS", "").strip()
    if not raw:
        return tuple(all_daemons)
    wanted = {n.strip() for n in raw.split(",") if n.strip()}
    return tuple(d for d in all_daemons if d.name in wanted)


def _log(msg: str) -> None:
    sys.stdout.write(f"gateway-init: {msg}\n")
    sys.stdout.flush()


def _pump(name: str, stream: IO[bytes]) -> None:
    """Copy `stream`'s lines to stdout prefixed `[name]`, on its own daemon
    thread."""
    try:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            sys.stdout.write(f"[{name}] {line}\n")
            sys.stdout.flush()
    except (OSError, ValueError) as exc:
        # The manager may close a dead child's pipe mid-read; that ends the
        # pump normally. Other I/O errors are still reported.
        if not stream.closed:
            _log(f"{name} output pump stopped: {type(exc).__name__}: {exc}")


def _spawn(spec: _DaemonSpec) -> subprocess.Popen[bytes]:
    env = _env_for_daemon(spec.name, dict(os.environ))
    proc = subprocess.Popen(  # pylint: disable=consider-using-with
        _argv_for_daemon(spec.name, spec.argv, env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        env=env,
    )
    threading.Thread(
        target=_pump, args=(spec.name, proc.stdout), daemon=True
    ).start()
    return proc


class _DaemonManager:
    """The running children and shutdown state, separate so tests can drive it."""

    def __init__(self, specs: Sequence[_DaemonSpec]):
        self.specs = tuple(specs)
        self.procs: list[tuple[_DaemonSpec, subprocess.Popen[bytes]]] = []
        self.shutdown_at: float | None = None
        # Children already logged as exited.
        self._logged_dead: set[str] = set()
        # Crashed children awaiting restart, drained by the main loop.
        self._restart_requested: set[str] = set()

    def start_all(self) -> None:
        for spec in self.specs:
            _log(f"starting {spec.name}")
            self.procs.append((spec, _spawn(spec)))

    def request_shutdown(self, reason: str) -> None:
        if self.shutdown_at is not None:
            return
        self.shutdown_at = time.monotonic()
        self._restart_requested.clear()
        _log(f"shutting down ({reason}); forwarding SIGTERM")
        for _, p in self.procs:
            if p.poll() is None:
                try:
                    p.terminate()
                except ProcessLookupError:
                    pass

    def _sigkill_all(self) -> None:
        for _, p in self.procs:
            if p.poll() is None:
                try:
                    p.kill()
                except ProcessLookupError:
                    pass

    def tick(self) -> bool:
        """One watch-loop iteration; True once every child has exited. A crash
        is logged and restarted; only a signal starts shutdown."""
        restarted_children = bool(self._restart_requested)
        self._drain_restart_requests()

        for spec, p in self.procs:
            rc = p.poll()
            if rc is None or spec.name in self._logged_dead:
                continue
            self._logged_dead.add(spec.name)
            if self.shutdown_at is None:
                _log(f"{spec.name} exited with code {rc}; scheduling restart")
                self._restart_requested.add(spec.name)
            else:
                _log(f"{spec.name} exited with code {rc}")

        # Process restarts before the all-done check, or a single-daemon
        # supervisor would exit with one queued.
        restarted_children |= bool(self._restart_requested)
        self._drain_restart_requests()

        if self.shutdown_at is not None:
            elapsed = time.monotonic() - self.shutdown_at
            if elapsed > _GRACE_SECONDS:
                still_running = [
                    spec.name for spec, p in self.procs if p.poll() is None
                ]
                if still_running:
                    _log(
                        f"grace ({_GRACE_SECONDS:.0f}s) elapsed; SIGKILL on "
                        f"{', '.join(still_running)}"
                    )
                    self._sigkill_all()

        done = (
            not restarted_children
            and all(p.poll() is not None for _, p in self.procs)
        )
        if done:
            for _, p in self.procs:
                if p.stdout is not None:
                    p.stdout.close()
        return done

    def exit_code(self) -> int:
        """A positive child failure wins; otherwise success (signal deaths
        aren't failures)."""
        positives = [
            p.returncode for _, p in self.procs
            if p.returncode is not None and p.returncode > 0
        ]
        return max(positives, default=0)

    def _drain_restart_requests(self) -> None:
        if self.shutdown_at is not None:
            self._restart_requested.clear()
            return
        requested = tuple(sorted(self._restart_requested))
        self._restart_requested.clear()
        for daemon_name in requested:
            if self.shutdown_at is not None:
                self._restart_requested.clear()
                return
            self.restart_daemon(daemon_name)

    def restart_daemon(self, daemon_name: str, *, grace: float = 5.0) -> bool:
        """SIGTERM one child, SIGKILL after `grace`, and spawn a replacement in
        its slot, leaving the others running. False if there's no such daemon."""
        if self.shutdown_at is not None:
            _log(f"restart {daemon_name} skipped; supervisor is shutting down")
            return False
        for idx, (spec, p) in enumerate(self.procs):
            if spec.name != daemon_name:
                continue
            _log(f"restarting {daemon_name}")
            if p.poll() is None:
                try:
                    p.terminate()
                except ProcessLookupError:
                    pass
                try:
                    p.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    _log(
                        f"{daemon_name} did not exit within {grace:.0f}s "
                        f"of SIGTERM; SIGKILL"
                    )
                    try:
                        p.kill()
                    except ProcessLookupError:
                        pass
                    p.wait()
            if p.stdout is not None:
                p.stdout.close()
            self._logged_dead.discard(daemon_name)
            new_proc = _spawn(spec)
            self.procs[idx] = (spec, new_proc)
            _log(f"{daemon_name} restarted (pid {new_proc.pid})")
            return True
        return False


def main(argv: Sequence[str] | None = None) -> int:
    del argv  # env-only
    specs = _selected_daemons(dict(os.environ))
    if not specs:
        _log("no daemons selected; nothing to do")
        return 0

    sup = _DaemonManager(specs)
    sup.start_all()

    signal.signal(signal.SIGTERM, lambda *_: sup.request_shutdown("SIGTERM"))  # type: ignore
    signal.signal(signal.SIGINT, lambda *_: sup.request_shutdown("SIGINT"))  # type: ignore

    while not sup.tick():
        time.sleep(_POLL_INTERVAL)

    rc = sup.exit_code()
    _log(f"exit {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
