"""git-gate deploy-key lifecycle for `gitea` upstreams (PRD 0047/0048).

Provisions a fresh ed25519 deploy key via the forge API at prepare time
and revokes it at teardown, so the agent never holds an upstream
credential. Split out of `git_gate.py`; the forge HTTP client is lazily
imported (`deploy_key_provisioner`) to keep its cost off the host path.
`git_gate` re-exports these names for API stability."""

from __future__ import annotations

import os
import dataclasses
import socket
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import MissingEnvVarError
from .log import info
from .manifest import ManifestBottle, ManifestGitEntry
from .git_gate_render import GitGateUpstream

if TYPE_CHECKING:
    from .git_gate import GitGatePlan

def _provision_dynamic_key(
    entry: ManifestGitEntry,
    slug: str,
    stage_dir: Path,
) -> str:
    """Generate a fresh ed25519 keypair, register the public half with
    the forge, and persist the private key + key ID under `stage_dir`.

    Returns the host-side path to the private key file so the caller
    can inject it into the GitGateUpstream as `identity_file`."""
    from .deploy_key_provisioner import get_provisioner
    pk = entry.Key
    token = os.environ.get(pk.forge_token_env)
    if token is None:
        raise MissingEnvVarError(
            pk.forge_token_env,
            f"git-gate.repos[{entry.Name!r}] key.forge_token_env"
            f" = {pk.forge_token_env!r}: env var is not set",
        )
    api_url = pk.api_url or f"https://{entry.UpstreamHost}"
    provisioner = get_provisioner(pk.provider, token, api_url)

    owner_repo = entry.UpstreamPath
    if owner_repo.endswith(".git"):
        owner_repo = owner_repo[:-4]
    title = f"bot-bottle:{socket.gethostname()}:{slug}:{entry.Name}"

    info(f"provisioning deploy key for git-gate.repos[{entry.Name!r}]")
    key_id, private_key_bytes = provisioner.create(owner_repo, title)

    key_file = stage_dir / f"{entry.Name}-key"
    key_file.write_bytes(private_key_bytes)
    key_file.chmod(0o600)

    id_file = stage_dir / f"{entry.Name}-deploy-key-id"
    id_file.write_text(key_id)
    id_file.chmod(0o600)

    info(f"provisioned deploy key {key_id} for git-gate.repos[{entry.Name!r}]")
    return str(key_file)


def revoke_git_gate_provisioned_keys(bottle: ManifestBottle, stage_dir: Path) -> None:
    """Revoke all deploy keys provisioned for `bottle` during prepare.

    Called at teardown after containers stop. Raises if any revocation
    fails — a stranded key is a security concern that the operator must
    address manually."""
    from .deploy_key_provisioner import get_provisioner
    for entry in bottle.git:
        if entry.Key.provider != "gitea":
            continue
        pk = entry.Key
        id_file = stage_dir / f"{entry.Name}-deploy-key-id"
        if not id_file.exists():
            continue
        key_id = id_file.read_text().strip()
        token = os.environ.get(pk.forge_token_env)
        if token is None:
            raise MissingEnvVarError(
                pk.forge_token_env,
                f"git-gate.repos[{entry.Name!r}] key.forge_token_env"
                f" = {pk.forge_token_env!r}: env var is not set;"
                f" cannot revoke deploy key {key_id}",
            )
        api_url = pk.api_url or f"https://{entry.UpstreamHost}"
        provisioner = get_provisioner(pk.provider, token, api_url)
        owner_repo = entry.UpstreamPath
        if owner_repo.endswith(".git"):
            owner_repo = owner_repo[:-4]
        info(f"revoking deploy key {key_id} for git-gate.repos[{entry.Name!r}]")
        provisioner.delete(owner_repo, key_id)
        info(f"revoked deploy key {key_id} for git-gate.repos[{entry.Name!r}]")


def _resolve_identity_file(entry: ManifestGitEntry, slug: str, stage_dir: Path) -> str:
    """Return the host-side SSH identity file path for this entry.
    For gitea entries, provisions a fresh deploy key first."""
    if entry.Key.provider == "gitea":
        return _provision_dynamic_key(entry, slug, stage_dir)
    return entry.IdentityFile


def provision_git_gate_dynamic_keys(
    bottle: ManifestBottle,
    plan: "GitGatePlan",
    stage_dir: Path,
) -> "GitGatePlan":
    """Provision dynamic git-gate keys and return an updated plan.

    This runs during backend launch, after the operator confirms the
    preflight. Plan preparation intentionally stays side-effect-light:
    dry-runs and aborted launches must not create remote deploy keys.
    """
    if not plan.upstreams:
        return plan

    upstreams_by_name: dict[str, GitGateUpstream] = {
        upstream.name: upstream for upstream in plan.upstreams
    }
    updated: list[GitGateUpstream] = []
    for entry in bottle.git:
        upstream = upstreams_by_name.get(entry.Name)
        if upstream is None:
            continue
        if entry.Key.provider == "gitea":
            identity_file = _provision_dynamic_key(entry, plan.slug, stage_dir)
            upstream = dataclasses.replace(upstream, identity_file=identity_file)
        updated.append(upstream)

    if len(updated) != len(plan.upstreams):
        updated_names = {u.name for u in updated}
        for upstream in plan.upstreams:
            if upstream.name not in updated_names:
                updated.append(upstream)

    return dataclasses.replace(plan, upstreams=tuple(updated))


__all__ = [
    "revoke_git_gate_provisioned_keys",
    "provision_git_gate_dynamic_keys",
    "_provision_dynamic_key",
    "_resolve_identity_file",
]
