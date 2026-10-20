"""Provision one bottle's git-gate state into the running shared gateway
(PRD 0070). Backend-neutral: it drives any `GatewayTransport` (docker/apple
exec+cp, firecracker SSH), and the guest-side paths it writes are identical
inside every backend's gateway because they all run the same gateway image.

The consolidated gateway serves every bottle's repos under `/git/<bottle_id>/`
with per-repo credentials under `/git-gate/creds/<bottle_id>/`. When a bottle
is registered the launcher must place *its* deploy keys + known_hosts into
that per-bottle creds dir and init its bare repos there — so this copies the
credential files into the live gateway and runs the (namespaced, init-only)
provisioning script produced by `git_gate_render_provision`.

Isolating each bottle's creds dir + repo root by id is what keeps one
bottle's push credentials out of another's repos on the shared gateway.
"""

from __future__ import annotations

import re

from ..git_gate import GitGatePlan, git_gate_render_provision
from ..control.gateway import GatewayProvisionError, GatewayTransport

# bottle ids index the gateway's per-bottle repo + creds dirs; they land in
# exec/cp path arguments, so validate before any path is built (a traversal
# id like "../etc" must never reach the gateway). Registry ids are token_hex —
# this is defense in depth at the transport boundary.
_SAFE_BOTTLE_ID = re.compile(r"[A-Za-z0-9_-]+")


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
    transport.cp_into(str(plan.gitleaks_config), "/etc/git-gate/gitleaks.toml")
    # The access-hook is exec'd directly (not via `sh`), so it needs the x bit.
    # Set it here rather than trusting the copy to carry the staged 0o700:
    # `docker cp` preserves source mode, but the Apple `container cp` does not,
    # landing the hook 0o644 → EACCES when the git-http handler tries to exec it.
    # chmod on the gateway side is backend-neutral and fixes every transport.
    transport.exec(["chmod", "+x", "/etc/git-gate/access-hook"])
    creds = _creds_dir(bottle_id)
    transport.exec(["mkdir", "-p", creds])
    transport.exec(["chmod", "700", creds])
    credential_paths: list[str] = []
    for u in plan.upstreams:
        if u.identity_file:
            key_path = f"{creds}/{u.name}-key"
            transport.cp_into(u.identity_file, key_path)
            credential_paths.append(key_path)
        known_hosts = str(u.known_hosts_file)
        if known_hosts and known_hosts != ".":
            known_hosts_path = f"{creds}/{u.name}-known_hosts"
            transport.cp_into(known_hosts, known_hosts_path)
            credential_paths.append(known_hosts_path)
    # Copy-mode behavior differs across Docker, Apple Container, and SSH.
    # Apply the security contract inside the gateway so every backend produces
    # the same private credential namespace.
    if credential_paths:
        transport.exec(["chmod", "600", *credential_paths])
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
    "GatewayProvisionError", "GatewayTransport",
]
