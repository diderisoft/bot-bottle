"""Provision one bottle's git-gate state into the running gateway, through any
`GatewayTransport`.

Each bottle's repos live under `/git/<bottle_id>/` and its credentials under
`/git-gate/creds/<bottle_id>/`. Separate directories keep one bottle's push
keys out of another's repos.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable

from ..control.git_gate_render import GitGateUpstream, git_gate_render_provision
from ..git_gate import GitGatePlan
from ..control.gateway import GatewayProvisionError, GatewayTransport

# GitHub repos over HTTPS (ADR 0014) take the user's token from here: tmpfs in
# the gateway, so it's in memory only, never on its disk or in a bottle.
TOKEN_ROOT = "/dev/shm/git-gate"
# Mounts tmpfs on /dev/shm if it isn't one (a firecracker infra VM has only
# devtmpfs on /dev), and refuses to go on if it still isn't.
_ENSURE_TMPFS = (
    "mkdir -p /dev/shm && "
    "{ grep -qs ' /dev/shm tmpfs ' /proc/mounts || "
    "mount -t tmpfs -o mode=1777,size=16m tmpfs /dev/shm; } && "
    "grep -qs ' /dev/shm tmpfs ' /proc/mounts")

# Bottle ids end up in exec/cp paths, so reject anything that could traverse.
_SAFE_BOTTLE_ID = re.compile(r"[A-Za-z0-9_-]+")


def _require_safe(bottle_id: str) -> None:
    if not _SAFE_BOTTLE_ID.fullmatch(bottle_id):
        raise GatewayProvisionError(f"unsafe bottle id {bottle_id!r}")


def _creds_dir(bottle_id: str) -> str:
    return f"/git-gate/creds/{bottle_id}"


def token_dir(bottle_id: str) -> str:
    return f"{TOKEN_ROOT}/{bottle_id}"


def github_token(secret: str) -> str | None:
    """The user's GitHub token from the host secrets file, renewed first when
    it's about to expire (`contrib/github/user_token.current_token`)."""
    # pylint: disable=import-outside-toplevel
    from ..contrib.github.user_token import current_token
    from ..paths import host_secrets_path
    from ..secret_source import FileSecrets
    return current_token(secret, FileSecrets(host_secrets_path()))


def push_tokens(transport: GatewayTransport, bottle_id: str,
                upstreams: tuple[GitGateUpstream, ...],
                token_for: Callable[[str], str | None] = github_token) -> None:
    """Put the token each HTTPS upstream needs into the gateway's tmpfs, as
    `<token_dir>/<secret>-token` (0600). Run at provision and again before a
    token expires. The token goes through a 0600 temp file the transport
    copies, never a command line; it's never logged."""
    _require_safe(bottle_id)
    secrets = sorted({u.token_secret for u in upstreams
                      if u.transport == "https" and u.token_secret})
    if not secrets:
        return
    try:
        transport.exec(["sh", "-c", _ENSURE_TMPFS])
    except GatewayProvisionError as e:
        raise GatewayProvisionError(
            "the gateway has no tmpfs for a GitHub token; refusing to put it on disk") from e
    directory = token_dir(bottle_id)
    transport.exec(["sh", "-c", f"umask 077 && mkdir -p {directory} && chmod 700 {directory}"])
    for secret in secrets:
        token = token_for(secret)
        if not token:
            raise GatewayProvisionError(
                f"no GitHub token in the host secret {secret}; connect GitHub on this host "
                "(`bot-bottle forge connect github`)")
        dest = f"{directory}/{secret}-token"
        fd, path = tempfile.mkstemp(prefix="bb-gh-token-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(token)
            transport.cp_into(path, dest)
        finally:
            os.unlink(path)
        transport.exec(["chmod", "600", dest])


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
    push_tokens(transport, bottle_id, plan.upstreams)
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
            "rm", "-rf", f"/git/{bottle_id}", _creds_dir(bottle_id), token_dir(bottle_id),
        ])
    except GatewayProvisionError:
        pass  # an absent namespace is success


__all__ = [
    "TOKEN_ROOT", "deprovision_git_gate", "github_token", "provision_git_gate", "push_tokens",
    "token_dir",
    "GatewayProvisionError", "GatewayTransport",
]
