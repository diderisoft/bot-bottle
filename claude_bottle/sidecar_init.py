"""Per-bottle sidecar supervisor (PRD 0024 chunk 1).

PID 1 inside the `claude-bottle-sidecars` bundle image. Spawns
the configured daemons (egress, pipelock, git-gate, supervise),
forwards SIGTERM/SIGINT to each child, and propagates per-daemon
stdout+stderr to the container log with a `[name] ` prefix.

Failure policy (interim): when a child dies unexpectedly, the
supervisor logs the death and leaves the surviving children
running. The bundle stays up; whatever the dead daemon served
will start failing, surfacing in the agent's own error path.
The supervisor itself exits only when (a) the operator/compose
sends SIGTERM/SIGINT, or (b) every child has died.

Failure policy (eventual): on unexpected death, the supervisor
restarts the daemon and emits a notification to the supervise
sidecar so the operator sees the event. That lands in a later
PR; the interim policy is "don't take the bundle down for one
sick daemon."

Daemon subset is env-driven. The compose renderer narrows it via
`CLAUDE_BOTTLE_SIDECAR_DAEMONS=egress,pipelock` for bottles that
don't use git-gate or supervise. Default: all four.

Stdlib-only by design — adding supervisord/s6/runit for four
daemons is heavier than this script.
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


# Below compose's default 10s `stop_grace_period`. After this many
# seconds past SIGTERM, escalate to SIGKILL on any still-running
# child.
_GRACE_SECONDS = 8.0

# Tight enough that exits and signals propagate without lag; loose
# enough that the main loop isn't a CPU hog.
_POLL_INTERVAL = 0.1


@dataclass(frozen=True)
class _DaemonSpec:
    name: str
    argv: Sequence[str]


# Order matters only for first-launch race-window reasons: egress
# starts first so pipelock's upstream connect succeeds during
# pipelock's own startup. git-gate and supervise are independent.
_DAEMONS: tuple[_DaemonSpec, ...] = (
    _DaemonSpec("egress", ("/bin/sh", "/app/egress-entrypoint.sh")),
    _DaemonSpec(
        "pipelock",
        ("/usr/local/bin/pipelock", "run", "--config", "/etc/pipelock.yaml"),
    ),
    _DaemonSpec("git-gate", ("/bin/sh", "/git-gate-entrypoint.sh")),
    _DaemonSpec("supervise", ("python3", "/app/supervise_server.py")),
)


def _selected_daemons(
    env: dict[str, str],
    all_daemons: Sequence[_DaemonSpec] | None = None,
) -> tuple[_DaemonSpec, ...]:
    """Filter the daemon set by the CLAUDE_BOTTLE_SIDECAR_DAEMONS env
    var. Unknown names in the list are ignored — the renderer is the
    source of truth for which daemons are wired.

    `all_daemons` defaults to `_DAEMONS` resolved at call time (not
    at definition time), so tests can monkey-patch the module-level
    `_DAEMONS` and have the new value take effect."""
    if all_daemons is None:
        all_daemons = _DAEMONS
    raw = env.get("CLAUDE_BOTTLE_SIDECAR_DAEMONS", "").strip()
    if not raw:
        return tuple(all_daemons)
    wanted = {n.strip() for n in raw.split(",") if n.strip()}
    return tuple(d for d in all_daemons if d.name in wanted)


def _log(msg: str) -> None:
    sys.stdout.write(f"sidecar-init: {msg}\n")
    sys.stdout.flush()


def _pump(name: str, stream: IO[bytes]) -> None:
    """Read lines from `stream`, prefix with `[name]`, write to
    stdout. Runs in its own thread per child; daemon=True so a
    blocked read doesn't keep the process alive after main exits."""
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        sys.stdout.write(f"[{name}] {line}\n")
        sys.stdout.flush()


def _spawn(spec: _DaemonSpec) -> subprocess.Popen:
    proc = subprocess.Popen(
        list(spec.argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    threading.Thread(
        target=_pump, args=(spec.name, proc.stdout), daemon=True
    ).start()
    return proc


class _Supervisor:
    """Holds the running children + shutdown state. Pulled out so
    the test suite can drive it with fake commands."""

    def __init__(self, specs: Sequence[_DaemonSpec]):
        self.specs = tuple(specs)
        self.procs: list[tuple[_DaemonSpec, subprocess.Popen]] = []
        self.shutdown_at: float | None = None
        # Names of children that have been logged as having exited
        # so we only log each death once across watch-loop ticks.
        self._logged_dead: set[str] = set()

    def start_all(self) -> None:
        for spec in self.specs:
            _log(f"starting {spec.name}")
            self.procs.append((spec, _spawn(spec)))

    def request_shutdown(self, reason: str) -> None:
        if self.shutdown_at is not None:
            return
        self.shutdown_at = time.monotonic()
        _log(f"shutting down ({reason}); forwarding SIGTERM")
        for _, p in self.procs:
            if p.poll() is None:
                try:
                    p.terminate()
                except ProcessLookupError:
                    pass

    def tick(self) -> bool:
        """One iteration of the watch loop. Returns True when every
        child has exited and the supervisor can return.

        A child dying unexpectedly is logged but does NOT initiate
        shutdown — see the module docstring's failure-policy
        section. Shutdown is signal-driven only."""
        for spec, p in self.procs:
            rc = p.poll()
            if rc is None or spec.name in self._logged_dead:
                continue
            self._logged_dead.add(spec.name)
            if self.shutdown_at is None:
                _log(
                    f"{spec.name} exited with code {rc}; leaving "
                    f"surviving daemons running (operator-visible "
                    f"via agent-side failure)"
                )
            else:
                _log(f"{spec.name} exited with code {rc}")

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
                    for _, p in self.procs:
                        if p.poll() is None:
                            try:
                                p.kill()
                            except ProcessLookupError:
                                pass

        return all(p.poll() is not None for _, p in self.procs)

    def exit_code(self) -> int:
        """Worst child returncode wins. On graceful shutdown every
        child is signal-killed (negative returncode) and max()
        returns 0; if some child crashed nonzero before the signal
        the operator gets that code on container exit."""
        return max((p.returncode for _, p in self.procs), default=0)


def main(argv: Sequence[str] | None = None) -> int:
    del argv  # no flags yet; env-driven only
    specs = _selected_daemons(dict(os.environ))
    if not specs:
        _log("no daemons selected; nothing to do")
        return 0

    sup = _Supervisor(specs)
    sup.start_all()

    signal.signal(signal.SIGTERM, lambda *_: sup.request_shutdown("SIGTERM"))
    signal.signal(signal.SIGINT, lambda *_: sup.request_shutdown("SIGINT"))

    while not sup.tick():
        time.sleep(_POLL_INTERVAL)

    rc = sup.exit_code()
    _log(f"exit {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
