"""git-gate deploy keys for `gitea` upstreams: minted through the forge API at
launch and revoked at teardown. The forge client is imported lazily."""

from __future__ import annotations

import json
import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

from ..bottle_state import globalize_slug
from ..errors import MissingEnvVarError
from ..lib.log import info
from ..manifest import ManifestBottle, ManifestGitEntry
from ..secret_source import SecretSource
from ..control.git_gate_render import GitGateUpstream

if TYPE_CHECKING:
    from .plan import GitGatePlan

def _provision_dynamic_key(
    entry: ManifestGitEntry,
    slug: str,
    stage_dir: Path,
    secrets: SecretSource,
) -> str:
    """Generate an ed25519 keypair, register the public half with the forge,
    store the private key and key id under `stage_dir`, and return the key's
    path."""
    from ..deploy_key_provisioner import get_provisioner
    pk = entry.Key
    token = secrets.get(pk.forge_token_env)
    if token is None:
        raise MissingEnvVarError(
            pk.forge_token_env,
            f"git-gate.repos[{entry.Name!r}] key.forge_token_env"
            f" = {pk.forge_token_env!r}: secret is not set",
        )
    api_url = pk.api_url or f"https://{entry.UpstreamHost}"
    provisioner = get_provisioner(pk.provider, token, api_url)

    owner_repo = entry.UpstreamPath
    if owner_repo.endswith(".git"):
        owner_repo = owner_repo[:-4]
    title = f"bot-bottle:{globalize_slug(slug)}:{entry.Name}"

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


def revoke_git_gate_provisioned_keys(
    bottle: ManifestBottle, stage_dir: Path, secrets: SecretSource,
) -> None:
    """Revoke every deploy key provisioned for `bottle`. Raises on any failure,
    since a stranded key needs the operator's attention."""
    from ..deploy_key_provisioner import get_provisioner
    for entry in bottle.git:
        if entry.Key.provider != "gitea":
            continue
        pk = entry.Key
        id_file = stage_dir / f"{entry.Name}-deploy-key-id"
        if not id_file.exists():
            continue
        key_id = id_file.read_text().strip()
        token = secrets.get(pk.forge_token_env)
        if token is None:
            raise MissingEnvVarError(
                pk.forge_token_env,
                f"git-gate.repos[{entry.Name!r}] key.forge_token_env"
                f" = {pk.forge_token_env!r}: secret is not set;"
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


def _resolve_identity_file(
    entry: ManifestGitEntry, slug: str, stage_dir: Path, secrets: SecretSource,
) -> str:
    """The entry's SSH identity file, provisioning a deploy key first for
    gitea."""
    if entry.Key.provider == "gitea":
        return _provision_dynamic_key(entry, slug, stage_dir, secrets)
    return entry.IdentityFile


def provision_git_gate_dynamic_keys(
    bottle: ManifestBottle,
    plan: "GitGatePlan",
    stage_dir: Path,
    secrets: SecretSource,
) -> "GitGatePlan":
    """Provision the gitea keys and return the updated plan. Runs at launch,
    after confirmation, so dry runs and aborted launches create no keys.
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
            identity_file = _provision_dynamic_key(entry, plan.slug, stage_dir, secrets)
            upstream = dataclasses.replace(upstream, identity_file=identity_file)
        updated.append(upstream)

    if len(updated) != len(plan.upstreams):
        updated_names = {u.name for u in updated}
        for upstream in plan.upstreams:
            if upstream.name not in updated_names:
                updated.append(upstream)

    final_plan = dataclasses.replace(plan, upstreams=tuple(updated))
    _write_upstreams_snapshot(stage_dir, final_plan.upstreams)
    return final_plan


def _write_upstreams_snapshot(
    stage_dir: Path, upstreams: tuple[GitGateUpstream, ...]
) -> None:
    """Write the resolved upstream table to `stage_dir/upstreams.json`, so a
    gateway restart can re-provision without the manifest."""
    data = [
        {
            "name": u.name,
            "upstream_url": u.upstream_url,
            "upstream_host": u.upstream_host,
            "upstream_port": u.upstream_port,
            "identity_file": u.identity_file,
            "known_host_key": u.known_host_key,
        }
        for u in upstreams
    ]
    snapshot = stage_dir / "upstreams.json"
    snapshot.write_text(json.dumps(data, indent=2))
    snapshot.chmod(0o600)


def load_upstreams_snapshot(stage_dir: Path) -> tuple[GitGateUpstream, ...]:
    """The upstreams `_write_upstreams_snapshot` saved, or () without
    upstreams, with each one's staged known_hosts when present."""
    snapshot = stage_dir / "upstreams.json"
    if not snapshot.is_file():
        return ()
    upstreams: list[GitGateUpstream] = []
    for u in json.loads(snapshot.read_text()):
        known_hosts = stage_dir / f"{u['name']}-known_hosts"
        upstreams.append(GitGateUpstream(
            name=u["name"],
            upstream_url=u["upstream_url"],
            upstream_host=u.get("upstream_host", ""),
            upstream_port=u.get("upstream_port", ""),
            identity_file=u.get("identity_file", ""),
            known_host_key=u.get("known_host_key", ""),
            known_hosts_file=known_hosts if known_hosts.is_file() else Path(),
        ))
    return tuple(upstreams)


__all__ = [
    "load_upstreams_snapshot",
    "revoke_git_gate_provisioned_keys",
    "provision_git_gate_dynamic_keys",
    "_provision_dynamic_key",
    "_resolve_identity_file",
]
