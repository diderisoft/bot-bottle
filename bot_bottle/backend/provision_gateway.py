"""Provision one bottle's git-gate state into the running gateway, through any
`GatewayTransport`.

Each bottle's repos live under `/git/<bottle_id>/` and its credentials under
`/git-gate/creds/<bottle_id>/`. Separate directories keep one bottle's push
keys out of another's repos.
"""

from __future__ import annotations

import re

from ..control.git_gate_render import git_gate_render_provision
from ..git_gate import GitGatePlan
from ..control.gateway import GatewayProvisionError, GatewayTransport

# Bottle ids end up in exec/cp paths, so reject anything that could traverse.
_SAFE_BOTTLE_ID = re.compile(r"[A-Za-z0-9_-]+")


def _require_safe(bottle_id: str) -> None:
    if not _SAFE_BOTTLE_ID.fullmatch(bottle_id):
        raise GatewayProvisionError(f"unsafe bottle id {bottle_id!r}")


def _creds_dir(bottle_id: str) -> str:
    return f"/git-gate/creds/{bottle_id}"


def provision_git_gate(
    transport: GatewayTransport, bottle_id: str, plan: GitGatePlan,
) -> None:
    """Copy `bottle_id`'s keys (and known_hosts) into
    `/git-gate/creds/<bottle_id>/` and init its bare repos under
    `/git/<bottle_id>/`. No-op without upstreams."""
    _require_safe(bottle_id)
    if not plan.upstreams:
        return
    # The hooks are shared by every bottle's repos; installing them is
    # idempotent.
    transport.exec(["mkdir", "-p", "/etc/git-gate"])
    transport.cp_into(str(plan.hook_script), "/etc/git-gate/pre-receive")
    transport.cp_into(str(plan.access_hook_script), "/etc/git-gate/access-hook")
    transport.cp_into(str(plan.gitleaks_config), "/etc/git-gate/gitleaks.toml")
    # The access hook is exec'd directly. Apple's `container cp` drops the
    # mode, so set it in the gateway.
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
    # Without a baseline file the repos name one that isn't there, and
    # git-gate refuses pushes as misconfigured.
    baseline = f"{creds}/baseline-commits"
    if plan.baseline_file.is_file():
        transport.cp_into(str(plan.baseline_file), baseline)
        credential_paths.append(baseline)
    # Copy modes differ per transport, so set credential permissions in the
    # gateway.
    if credential_paths:
        transport.exec(["chmod", "600", *credential_paths])
    # Init the bare repos and per-repo credential config.
    script = git_gate_render_provision(
        bottle_id, plan.upstreams,
        trailer_mode=plan.trailer_mode, trailer=plan.trailer, baseline=baseline)
    transport.exec(["sh", "-c", script])


def deprovision_git_gate(transport: GatewayTransport, bottle_id: str) -> None:
    """Remove a bottle's repos and credentials from the gateway. Best effort
    and idempotent."""
    _require_safe(bottle_id)
    try:
        transport.exec([
            "rm", "-rf", f"/git/{bottle_id}", _creds_dir(bottle_id),
        ])
    except GatewayProvisionError:
        pass  # an absent namespace is success


__all__ = [
    "provision_git_gate", "deprovision_git_gate",
    "GatewayProvisionError", "GatewayTransport",
]
