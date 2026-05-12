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

from .manifest import Bottle

# First listen port on the gate; entry i listens on BASE_LISTEN_PORT + i.
# Picked high enough to avoid colliding with anything an alpine image
# might pre-bind. The port space is per-gate (gate is per-agent) so
# collisions across bottles aren't possible.
BASE_LISTEN_PORT = 30000


@dataclass(frozen=True)
class SSHGateUpstream:
    """One forwarder rule on the gate: listen locally on `listen_port`,
    forward each connection to `upstream_host:upstream_port`. The
    `bottle_host_alias` is the `Host` value from the manifest entry,
    kept for diagnostics + so the ssh provisioner can correlate
    upstreams with their alias."""

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
    """Deterministic assignment of listen ports to bottle.ssh entries:
    BASE_LISTEN_PORT + index. Order matches `bottle.ssh` so a manifest
    re-order yields a different port mapping (intentional — the
    provisioner reads the same tuple)."""
    return tuple(
        SSHGateUpstream(
            listen_port=BASE_LISTEN_PORT + i,
            upstream_host=e.Hostname,
            upstream_port=e.Port,
            bottle_host_alias=e.Host,
        )
        for i, e in enumerate(bottle.ssh)
    )


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
