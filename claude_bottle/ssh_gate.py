"""Per-agent SSH egress gate (PRD 0007).

A second per-agent sidecar that does plain TCP forwarding from a set
of static listen ports to the SSH hosts declared in `bottle.ssh`.
The agent's ssh client points each `Host` block at the gate
container + a per-entry listen port; pipelock stops seeing SSH
traffic entirely.

This module defines the abstract gate (`SSHGate`) and the plan
dataclass (`SSHGatePlan`) consumed by its `start`. The sidecar's
start/stop lifecycle is backend-specific and lives on concrete
subclasses (see `claude_bottle/backend/docker/ssh_gate.py`)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from .log import die
from .manifest import Bottle

# Default port when an ssh entry has no `Port` field. Matches OpenSSH.
_DEFAULT_SSH_PORT = 22


@dataclass(frozen=True)
class SSHGateUpstream:
    """One forwarder rule on the gate: listen locally on `listen_port`,
    forward each connection to `upstream_host:upstream_port`. The
    `bottle_host_alias` is the `Host` value from the manifest entry,
    kept for diagnostics + so the ssh provisioner can correlate
    upstreams with their alias.

    `listen_port` mirrors the upstream port. That choice lets git
    URLs that bake the upstream port into the remote (e.g.
    `ssh://git@host:30009/repo.git`) work without rewriting: OpenSSH
    treats a URL-supplied port as overriding the config's `Port`
    directive, so the gate must be reachable on the same port the URL
    names. Two ssh entries that share an upstream port are a config
    error and rejected at prepare time."""

    listen_port: int
    upstream_host: str
    upstream_port: str
    bottle_host_alias: str


@dataclass(frozen=True)
class SSHGatePlan:
    """Output of SSHGate.prepare; consumed by .start when the sidecar
    needs to be brought up.

    `upstreams` + `slug` + `entrypoint_script` are filled in at
    prepare time (host-side, side-effect-free on docker). The network
    fields are populated by the backend's launch step via
    `dataclasses.replace` once those networks exist. Empty defaults
    are sentinels meaning "not yet set"; `.start` validates that
    they are populated."""

    slug: str
    entrypoint_script: Path
    upstreams: tuple[SSHGateUpstream, ...]
    internal_network: str = ""
    egress_network: str = ""


def ssh_gate_upstreams_for_bottle(bottle: Bottle) -> tuple[SSHGateUpstream, ...]:
    """Build the gate's upstream table. Each ssh entry's listen port
    equals its upstream port so URL-supplied ports (which override
    `~/.ssh/config`'s `Port` directive) still reach the gate.

    Dies on two entries sharing an upstream port — the gate is a
    single container with a flat port space, so each listener has to
    be unique."""
    seen_ports: dict[int, str] = {}
    upstreams: list[SSHGateUpstream] = []
    for e in bottle.ssh:
        port = int(e.Port) if e.Port else _DEFAULT_SSH_PORT
        if port in seen_ports:
            die(
                f"ssh entries '{seen_ports[port]}' and '{e.Host}' share upstream port "
                f"{port}; the per-agent ssh gate can only forward one upstream "
                f"per port. Change one of the upstream Ports in claude-bottle.json."
            )
        seen_ports[port] = e.Host
        upstreams.append(
            SSHGateUpstream(
                listen_port=port,
                upstream_host=e.Hostname,
                upstream_port=e.Port,
                bottle_host_alias=e.Host,
            )
        )
    return tuple(upstreams)


def ssh_gate_render_entrypoint(upstreams: tuple[SSHGateUpstream, ...]) -> str:
    """Render the gate's entrypoint script: one `socat TCP-LISTEN`
    per upstream, all backgrounded, then `wait`. Posix sh, no bash-isms
    (alpine's sh is busybox ash). If any one socat dies, the others
    keep running until the container is removed — matches the v1
    no-restart policy from the PRD."""
    lines = ["#!/bin/sh", "set -eu"]
    for u in upstreams:
        lines.append(
            f"socat TCP-LISTEN:{u.listen_port},reuseaddr,fork "
            f"TCP:{u.upstream_host}:{u.upstream_port} &"
        )
    lines.append("wait")
    return "\n".join(lines) + "\n"


class SSHGate(ABC):
    """The per-agent SSH egress gate. Encapsulates the host-side
    prepare step (upstream allocation + entrypoint render); the
    sidecar's start/stop lifecycle is backend-specific and lives on
    concrete subclasses."""

    def prepare(self, bottle: Bottle, slug: str, stage_dir: Path) -> SSHGatePlan:
        """Compute the upstream table from `bottle.ssh` and write the
        entrypoint script (mode 600) under `stage_dir`. Pure host-side,
        no docker subprocess.

        Returned plan is incomplete: the launch step must fill
        `internal_network` / `egress_network` via `dataclasses.replace`
        before passing the plan to `.start`."""
        upstreams = ssh_gate_upstreams_for_bottle(bottle)
        script = stage_dir / "ssh_gate_entrypoint.sh"
        script.write_text(ssh_gate_render_entrypoint(upstreams))
        script.chmod(0o600)
        return SSHGatePlan(slug=slug, entrypoint_script=script, upstreams=upstreams)

    @abstractmethod
    def start(self, plan: SSHGatePlan) -> str:
        """Bring up the gate sidecar according to `plan`. Returns the
        target string identifying the running instance — the same
        value to pass to `.stop`. Backend-specific."""

    @abstractmethod
    def stop(self, target: str) -> None:
        """Tear down the gate sidecar identified by `target` (the
        value `.start` returned). Idempotent: a missing target is
        success. Backend-specific."""
