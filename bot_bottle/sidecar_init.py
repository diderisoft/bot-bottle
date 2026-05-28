"""Per-bottle sidecar supervisor (PRD 0024 chunk 1).

PID 1 inside the `bot-bottle-sidecars` bundle image. Spawns
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
`BOT_BOTTLE_SIDECAR_DAEMONS=egress,pipelock` for bottles that
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


# Env-var name prefixes that carry egress-only credentials.
# `egress_apply.py` assigns `EGRESS_TOKEN_<n>` slots that egress
# reads to inject `Authorization` headers on configured routes;
# every other daemon in the bundle (especially pipelock with
# `scan_env: true`) MUST NOT see these values or it'll match the
# injected token in the request egress just sent and 403-block
# the legitimate traffic (issue #84). The agent itself runs in a
# different machine and never has access to these slots in the
# first place, so stripping them from non-egress daemons loses no
# DLP coverage — pipelock can't catch the exfil of a value the
# agent doesn't have.
_EGRESS_ONLY_ENV_PREFIXES: tuple[str, ...] = ("EGRESS_TOKEN_",)


def _env_for_daemon(name: str, base_env: dict[str, str]) -> dict[str, str]:
    """Egress sees the full bundle env. Everyone else gets a copy
    with `EGRESS_TOKEN_*` (and any other future egress-only
    credential slots) stripped. Returns a fresh dict — callers
    can mutate without affecting `base_env`."""
    if name == "egress":
        return dict(base_env)
    return {
        k: v for k, v in base_env.items()
        if not any(k.startswith(p) for p in _EGRESS_ONLY_ENV_PREFIXES)
    }


# Order matters only for first-launch race-window reasons: egress
# starts first so pipelock's upstream connect succeeds during
# pipelock's own startup. git-gate and supervise are independent.
# Pipelock binds 0.0.0.0:8888 explicitly. Without `--listen` it
# defaults to 127.0.0.1 which would be unreachable from sibling
# services on the docker network. The legacy four-sidecar
# compose renderer passed the same flag; the bundle keeps the
# explicit binding.
_DAEMONS: tuple[_DaemonSpec, ...] = (
    _DaemonSpec("egress", ("/bin/sh", "/app/egress-entrypoint.sh")),
    _DaemonSpec(
        "pipelock",
        ("/usr/local/bin/pipelock", "run",
         "--config", "/etc/pipelock.yaml",
         "--listen", "0.0.0.0:8888"),
    ),
    _DaemonSpec("git-gate", ("/bin/sh", "/git-gate-entrypoint.sh")),
    _DaemonSpec("supervise", ("python3", "/app/supervise_server.py")),
)


def _selected_daemons(
    env: dict[str, str],
    all_daemons: Sequence[_DaemonSpec] | None = None,
) -> tuple[_DaemonSpec, ...]:
    """Filter the daemon set by the BOT_BOTTLE_SIDECAR_DAEMONS env
    var. Unknown names in the list are ignored — the renderer is the
    source of truth for which daemons are wired.

    `all_daemons` defaults to `_DAEMONS` resolved at call time (not
    at definition time), so tests can monkey-patch the module-level
    `_DAEMONS` and have the new value take effect."""
    if all_daemons is None:
        all_daemons = _DAEMONS
    raw = env.get("BOT_BOTTLE_SIDECAR_DAEMONS", "").strip()
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
        env=_env_for_daemon(spec.name, dict(os.environ)),
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

    def forward_signal(self, sig: int, daemon_name: str) -> bool:
        """Forward a signal to one named child. Used by the SIGHUP
        path: egress_apply.py runs `docker kill --signal HUP
        <bundle>`, the host kernel delivers SIGHUP to PID 1 (this
        supervisor), and we relay it to mitmdump so it reloads
        its addon's routes.yaml. Returns True iff a live child by
        that name was signaled."""
        for spec, p in self.procs:
            if spec.name != daemon_name:
                continue
            if p.poll() is not None:
                _log(
                    f"SIGHUP for {daemon_name} dropped; daemon "
                    f"already exited (rc={p.returncode})"
                )
                return False
            try:
                p.send_signal(sig)
            except ProcessLookupError:
                return False
            _log(f"forwarded {signal.Signals(sig).name} to {daemon_name}")
            return True
        return False

    def restart_daemon(self, daemon_name: str, *, grace: float = 5.0) -> bool:
        """Terminate one named child and spawn a fresh one, leaving
        the other daemons running. Used by the pipelock-apply path:
        pipelock has no in-process reload, so apply_allowlist_change
        runs `docker kill --signal USR1 <bundle>` after writing the
        new yaml; the supervisor catches SIGUSR1 and calls this.

        Behavior: SIGTERM → wait up to `grace` seconds → SIGKILL if
        still alive → spawn a replacement under the same DaemonSpec.
        The `procs` slot is updated in place so subsequent
        forward_signal / shutdown calls reach the new pid.

        Returns True iff a daemon by that name was running and a
        replacement spawned; False if no such daemon (the
        compose-renderer subset said this bottle doesn't run it)."""
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
            self._logged_dead.discard(daemon_name)
            new_proc = _spawn(spec)
            self.procs[idx] = (spec, new_proc)
            _log(f"{daemon_name} restarted (pid {new_proc.pid})")
            return True
        return False


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
    # SIGHUP reload path: egress_apply.py runs `docker kill
    # --signal HUP <bundle>` after writing routes.yaml. The kernel
    # delivers SIGHUP to PID 1 (this supervisor); forward it to
    # mitmdump so it reloads its addon.
    signal.signal(signal.SIGHUP, lambda *_: sup.forward_signal(signal.SIGHUP, "egress"))
    # SIGUSR1 pipelock-restart path: pipelock_apply.py runs
    # `docker kill --signal USR1 <bundle>` after writing
    # pipelock.yaml. Pipelock has no in-process reload, so the
    # supervisor restarts the pipelock daemon in place (other
    # daemons keep running — specifically supervise, whose MCP
    # socket would drop on a whole-container `docker restart`).
    signal.signal(signal.SIGUSR1, lambda *_: sup.restart_daemon("pipelock"))

    while not sup.tick():
        time.sleep(_POLL_INTERVAL)

    rc = sup.exit_code()
    _log(f"exit {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
