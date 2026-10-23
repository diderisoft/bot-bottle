"""Gateway data-plane supervisor.

PID 1 inside the `bot-bottle-gateway` data-plane image. Spawns
the configured daemons (egress, git-gate, supervise),
forwards SIGTERM/SIGINT to each child, and propagates per-daemon
stdout+stderr to the container log with a `[name] ` prefix.

Failure policy: when a child dies unexpectedly, the supervisor
restarts it automatically and logs the restart. The gateway stays
up; a temporary loss of one daemon (e.g. egress OOM-killed) is
recovered without manual container recreation. The supervisor
itself exits only when the operator sends SIGTERM/SIGINT.

Daemon subset is env-driven via `BOT_BOTTLE_GATEWAY_DAEMONS=egress`
for callers that don't use git-gate or supervise. Default: all
daemons.

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
# no other daemon in the bundle should see these values.
_EGRESS_ONLY_ENV_PREFIXES: tuple[str, ...] = ("EGRESS_TOKEN_",)
_READY_GATED_DAEMONS: tuple[str, ...] = ("git-gate", "git-http")

# The control-plane signing key is the orchestrator's alone (it verifies
# tokens), and the orchestrator runs in a separate container/VM — the gateway
# only ever holds the pre-minted `gateway` JWT its daemons present. Strip the
# key from every daemon's env as defense-in-depth, so a compromised data-plane
# daemon can't read it and mint a `cli` token even if it somehow leaked into the
# gateway container. Value matches
# contract.identity.ORCHESTRATOR_TOKEN_ENV; hardcoded here so this supervisor stays
# import-light.
_SIGNING_KEY_ENV = "BOT_BOTTLE_ORCHESTRATOR_TOKEN"


def _env_for_daemon(name: str, base_env: dict[str, str]) -> dict[str, str]:
    """Per-daemon env, scoped to what each process legitimately needs.
    Returns a fresh dict — callers can mutate without affecting `base_env`.

      * `EGRESS_TOKEN_*` upstream-auth slots go to egress only.
      * the control-plane signing key is stripped from every daemon — the
        gateway never holds it (it's the orchestrator's); the data-plane daemons
        present the pre-minted `gateway` JWT instead."""
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
    _DaemonSpec("git-gate", ("/bin/sh", "/git-gate-entrypoint.sh")),
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
    """Filter the daemon set by the BOT_BOTTLE_GATEWAY_DAEMONS env var.

    When the var is unset/empty, return all daemons (the standard gateway
    subset). Unknown names are ignored.

    `all_daemons` defaults to `_DAEMONS` resolved at call time (not at
    definition time), so tests can pass a custom list."""
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
    """Read lines from `stream`, prefix with `[name]`, write to
    stdout. Runs in its own thread per child; daemon=True so a
    blocked read doesn't keep the process alive after main exits."""
    try:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            sys.stdout.write(f"[{name}] {line}\n")
            sys.stdout.flush()
    except (OSError, ValueError) as exc:
        # The manager closes a dead child's pipe after wait() and before a
        # restart. A pump can be between readline calls at that exact moment;
        # closed-stream errors are normal completion, not uncaught thread
        # failures. Preserve genuinely unexpected I/O diagnostics.
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
    """Holds the running children + shutdown state. Pulled out so
    the test suite can drive it with fake commands."""

    def __init__(self, specs: Sequence[_DaemonSpec]):
        self.specs = tuple(specs)
        self.procs: list[tuple[_DaemonSpec, subprocess.Popen[bytes]]] = []
        self.shutdown_at: float | None = None
        # Names of children that have been logged as having exited
        # so we only log each death once across watch-loop ticks.
        self._logged_dead: set[str] = set()
        # Signal handlers add daemon names here and return quickly.
        # The main watch loop drains the set, so repeated restart
        # requests for one daemon coalesce into one restart.
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

    def request_restart(self, daemon_name: str) -> bool:
        """Queue a daemon restart for the main loop to process.

        Signal handlers use this non-blocking path instead of doing
        subprocess lifecycle work directly. Requests coalesce by
        daemon name: one pending restart is enough to make the daemon
        reread the latest config from disk.

        Returns True iff a daemon by that name is known to the
        supervisor and shutdown has not started."""
        if self.shutdown_at is not None:
            _log(f"restart {daemon_name} skipped; supervisor is shutting down")
            return False
        if not any(spec.name == daemon_name for spec, _ in self.procs):
            return False
        self._restart_requested.add(daemon_name)
        return True

    def tick(self) -> bool:
        """One iteration of the watch loop. Returns True when every
        child has exited and the supervisor can return.

        A child dying unexpectedly is logged and restarted but does
        NOT initiate shutdown — see the module docstring's
        failure-policy section. Shutdown is signal-driven only."""
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

        # Restart deaths discovered above before checking whether all
        # processes are done. Deferring this until the next tick would make a
        # single-daemon supervisor return True and exit with the restart still
        # queued.
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
        """Positive child failures win; otherwise report success.

        Python represents signal-terminated children as negative
        return codes. A signal-only graceful shutdown should not leak
        that platform-specific detail into the container exit status,
        but a positive crash before shutdown should remain visible."""
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
        the other daemons running. A daemon that has no in-process
        reload can be restarted this way after its config file changes.

        Behavior: SIGTERM → wait up to `grace` seconds → SIGKILL if
        still alive → spawn a replacement under the same DaemonSpec.
        The `procs` slot is updated in place so subsequent
        forward_signal / shutdown calls reach the new pid.

        Returns True iff a daemon by that name was running and a
        replacement spawned; False if no such daemon (not wired
        for this bottle)."""
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
    del argv  # no flags yet; env-driven only
    specs = _selected_daemons(dict(os.environ))
    if not specs:
        _log("no daemons selected; nothing to do")
        return 0

    sup = _DaemonManager(specs)
    sup.start_all()

    signal.signal(signal.SIGTERM, lambda *_: sup.request_shutdown("SIGTERM"))  # type: ignore
    signal.signal(signal.SIGINT, lambda *_: sup.request_shutdown("SIGINT"))  # type: ignore
    # SIGHUP reload path: egress_apply.py runs `docker kill
    # --signal HUP <bundle>` after writing routes.yaml. The kernel
    # delivers SIGHUP to PID 1 (this supervisor); forward it to
    # mitmdump so it reloads its addon.
    signal.signal(
        signal.SIGHUP,
        lambda *_: sup.forward_signal(signal.SIGHUP, "egress"),  # type: ignore[misc]
    )

    while not sup.tick():
        time.sleep(_POLL_INTERVAL)

    rc = sup.exit_code()
    _log(f"exit {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
