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

from ...docker_cmd import run_docker
from ...git_gate import GitGatePlan, git_gate_render_provision

# bottle ids index the gateway's per-bottle repo + creds dirs; they land in
# `docker cp`/`rm` path arguments, so validate before any path is built (a
# traversal id like "../etc" must never reach the container). Registry ids are
# token_hex — this is defense in depth at the docker boundary.
_SAFE_BOTTLE_ID = re.compile(r"[A-Za-z0-9_-]+")


class GatewayProvisionError(RuntimeError):
    """A git-gate provisioning step against the running gateway failed."""


def _require_safe(bottle_id: str) -> None:
    if not _SAFE_BOTTLE_ID.fullmatch(bottle_id):
        raise GatewayProvisionError(f"unsafe bottle id {bottle_id!r}")


def _creds_dir(bottle_id: str) -> str:
    return f"/git-gate/creds/{bottle_id}"


def _exec(gateway: str, argv: list[str]) -> None:
    """`docker exec` a command in the gateway, raising on non-zero exit."""
    proc = run_docker(["docker", "exec", gateway, *argv])
    if proc.returncode != 0:
        raise GatewayProvisionError(
            f"gateway exec {argv!r} failed: {proc.stderr.strip()}"
        )


def _cp_into(gateway: str, src: str, dest: str) -> None:
    """`docker cp` a host file into the gateway, raising on non-zero exit."""
    proc = run_docker(["docker", "cp", src, f"{gateway}:{dest}"])
    if proc.returncode != 0:
        raise GatewayProvisionError(
            f"gateway cp {src} -> {dest} failed: {proc.stderr.strip()}"
        )


def provision_git_gate(gateway: str, bottle_id: str, plan: GitGatePlan) -> None:
    """Place `bottle_id`'s git-gate credentials into the running `gateway`
    container and init its bare repos under `/git/<bottle_id>/`.

    Copies each upstream's identity key (and known_hosts, when present) into
    `/git-gate/creds/<bottle_id>/`, then runs the namespaced provisioning
    script. No-op for a bottle with no git upstreams."""
    _require_safe(bottle_id)
    if not plan.upstreams:
        return
    creds = _creds_dir(bottle_id)
    _exec(gateway, ["mkdir", "-p", creds])
    for u in plan.upstreams:
        if u.identity_file:
            _cp_into(gateway, u.identity_file, f"{creds}/{u.name}-key")
        known_hosts = str(u.known_hosts_file)
        if known_hosts and known_hosts != ".":
            _cp_into(gateway, known_hosts, f"{creds}/{u.name}-known_hosts")
    # Init the bare repos + per-repo credential config for this namespace.
    script = git_gate_render_provision(bottle_id, plan.upstreams)
    _exec(gateway, ["sh", "-c", script])


def deprovision_git_gate(gateway: str, bottle_id: str) -> None:
    """Remove a bottle's repos + creds from the gateway on teardown. Idempotent
    — an already-absent namespace is a clean no-op (best effort; a stray dir
    can't leak, since attribution is by source IP and the bottle is gone)."""
    _require_safe(bottle_id)
    run_docker([
        "docker", "exec", gateway, "rm", "-rf",
        f"/git/{bottle_id}", _creds_dir(bottle_id),
    ])


__all__ = ["provision_git_gate", "deprovision_git_gate", "GatewayProvisionError"]
