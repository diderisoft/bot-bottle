"""Provision one bottle's git-gate state into the running shared gateway
(PRD 0070, docker slice).

The consolidated gateway serves every bottle's repos under `/git/<bottle_id>/`
with per-repo credentials under `/git-gate/creds/<bottle_id>/`. When a bottle
is registered the launcher must place *its* deploy keys + known_hosts into
that per-bottle creds dir and init its bare repos there — so this copies the
credential files into the live gateway container and runs the (namespaced,
init-only) provisioning script produced by `git_gate_render_provision`.

Isolating each bottle's creds dir + repo root by id is what keeps one
bottle's push credentials out of another's repos on the shared gateway.
"""

from __future__ import annotations

import re
from typing import Protocol

from ...docker_cmd import run_docker
from ...git_gate import GitGatePlan, git_gate_render_provision

# bottle ids index the gateway's per-bottle repo + creds dirs; they land in
# exec/cp path arguments, so validate before any path is built (a traversal
# id like "../etc" must never reach the gateway). Registry ids are token_hex —
# this is defense in depth at the transport boundary.
_SAFE_BOTTLE_ID = re.compile(r"[A-Za-z0-9_-]+")


class GatewayProvisionError(RuntimeError):
    """A git-gate provisioning step against the running gateway failed."""


class GatewayTransport(Protocol):
    """How the launcher stages files + runs commands in the running gateway.
    Backend-neutral so the same provisioning logic serves the docker gateway
    (exec/cp over the docker socket) and the firecracker gateway VM (over
    SSH)."""

    def exec(self, argv: list[str]) -> None:
        """Run `argv` in the gateway, raising `GatewayProvisionError` on
        failure."""

    def cp_into(self, src: str, dest: str) -> None:
        """Copy host file `src` to `dest` in the gateway, raising on
        failure."""


class DockerGatewayTransport:
    """`GatewayTransport` for the docker gateway container (exec/cp)."""

    def __init__(self, gateway: str) -> None:
        self.gateway = gateway

    def exec(self, argv: list[str]) -> None:
        proc = run_docker(["docker", "exec", self.gateway, *argv])
        if proc.returncode != 0:
            raise GatewayProvisionError(
                f"gateway exec {argv!r} failed: {proc.stderr.strip()}"
            )

    def cp_into(self, src: str, dest: str) -> None:
        proc = run_docker(["docker", "cp", src, f"{self.gateway}:{dest}"])
        if proc.returncode != 0:
            raise GatewayProvisionError(
                f"gateway cp {src} -> {dest} failed: {proc.stderr.strip()}"
            )


def _require_safe(bottle_id: str) -> None:
    if not _SAFE_BOTTLE_ID.fullmatch(bottle_id):
        raise GatewayProvisionError(f"unsafe bottle id {bottle_id!r}")


def _creds_dir(bottle_id: str) -> str:
    return f"/git-gate/creds/{bottle_id}"


def provision_git_gate(
    transport: GatewayTransport, bottle_id: str, plan: GitGatePlan,
) -> None:
    """Place `bottle_id`'s git-gate credentials into the running gateway and
    init its bare repos under `/git/<bottle_id>/`.

    Copies each upstream's identity key (and known_hosts, when present) into
    `/git-gate/creds/<bottle_id>/`, then runs the namespaced provisioning
    script. No-op for a bottle with no git upstreams."""
    _require_safe(bottle_id)
    if not plan.upstreams:
        return
    # The pre-receive + access hooks are bottle-agnostic and shared by every
    # bottle's repos; install them into the gateway (idempotent — same content
    # each time). The per-bottle model cp'd these into each bundle at start.
    transport.exec(["mkdir", "-p", "/etc/git-gate"])
    transport.cp_into(str(plan.hook_script), "/etc/git-gate/pre-receive")
    transport.cp_into(str(plan.access_hook_script), "/etc/git-gate/access-hook")
    creds = _creds_dir(bottle_id)
    transport.exec(["mkdir", "-p", creds])
    for u in plan.upstreams:
        if u.identity_file:
            transport.cp_into(u.identity_file, f"{creds}/{u.name}-key")
        known_hosts = str(u.known_hosts_file)
        if known_hosts and known_hosts != ".":
            transport.cp_into(known_hosts, f"{creds}/{u.name}-known_hosts")
    # Init the bare repos + per-repo credential config for this namespace.
    script = git_gate_render_provision(bottle_id, plan.upstreams)
    transport.exec(["sh", "-c", script])


def deprovision_git_gate(transport: GatewayTransport, bottle_id: str) -> None:
    """Remove a bottle's repos + creds from the gateway on teardown. Idempotent
    — an already-absent namespace is a clean no-op (best effort; a stray dir
    can't leak, since attribution is by source IP and the bottle is gone)."""
    _require_safe(bottle_id)
    try:
        transport.exec([
            "rm", "-rf", f"/git/{bottle_id}", _creds_dir(bottle_id),
        ])
    except GatewayProvisionError:
        pass  # best-effort teardown; absent namespace is success


__all__ = [
    "provision_git_gate", "deprovision_git_gate",
    "GatewayProvisionError", "GatewayTransport", "DockerGatewayTransport",
]
