"""git-gate deploy keys for forge (`gitea`, `github`) upstreams: minted
through the forge's API at launch and revoked at teardown. The forge client is imported lazily."""

from __future__ import annotations

import json
import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

from ..bottle_state import globalize_slug
from ..contract.forges import FORGE_KINDS
from ..errors import MissingEnvVarError
from ..lib.log import info, warn
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
    *,
    title_suffix: str = "",
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
    # A re-grant mints its key while the old one still exists, and the forge
    # wants titles unique per repo, so it's titled with its access.
    title = f"bot-bottle:{globalize_slug(slug)}:{entry.Name}{title_suffix}"

    info(f"provisioning deploy key for git-gate.repos[{entry.Name!r}]")
    key_id, private_key_bytes = provisioner.create(owner_repo, title,
                                                   read_only=pk.read_only)

    key_file = stage_dir / f"{entry.Name}-key"
    key_file.write_bytes(private_key_bytes)
    key_file.chmod(0o600)

    id_file = stage_dir / f"{entry.Name}-deploy-key-id"
    id_file.write_text(key_id)
    id_file.chmod(0o600)

    info(f"provisioned deploy key {key_id} for git-gate.repos[{entry.Name!r}]")
    return str(key_file)


@dataclasses.dataclass(frozen=True)
class DeployKeyRef:
    """What revoking a repo's forge deploy key needs, minus the token: safe to
    write to disk."""

    name: str
    forge_token_env: str
    api_url: str
    upstream_host: str
    upstream_path: str
    # A live grant's `<forge>/<owner>/<repo>[:write]`, so it can be kept.
    grant: str = ""
    # The forge kind that minted it; records from before GitHub are Gitea's.
    provider: str = "gitea"


def deploy_key_refs(bottle: ManifestBottle) -> tuple[DeployKeyRef, ...]:
    """The bottle's repos whose deploy keys are minted per launch."""
    return tuple(
        DeployKeyRef(name=e.Name, forge_token_env=e.Key.forge_token_env,
                     api_url=e.Key.api_url, upstream_host=e.UpstreamHost,
                     upstream_path=e.UpstreamPath, provider=e.Key.provider)
        for e in bottle.git if e.Key.provider in FORGE_KINDS
    )


_LIVE_GRANTS = "live-grants.json"


def live_grant_refs(stage_dir: Path) -> tuple[DeployKeyRef, ...]:
    """The deploy keys granted to the running bottle after launch
    (`live_grant`), revoked at teardown with the launch's own."""
    try:
        raw = json.loads((stage_dir / _LIVE_GRANTS).read_text())
    except (OSError, ValueError):
        return ()
    return tuple(DeployKeyRef(**r) for r in raw if isinstance(r, dict))


def record_live_grant(stage_dir: Path, ref: DeployKeyRef) -> None:
    """Record `ref`, replacing one for the same repo name (a re-grant)."""
    path = stage_dir / _LIVE_GRANTS
    refs = [*(r for r in live_grant_refs(stage_dir) if r.name != ref.name), ref]
    path.write_text(json.dumps([dataclasses.asdict(r) for r in refs], indent=2))
    path.chmod(0o600)


def forget_live_grant(stage_dir: Path, name: str) -> None:
    """Drop the live grant of the repo `name` from the record."""
    path = stage_dir / _LIVE_GRANTS
    refs = [r for r in live_grant_refs(stage_dir) if r.name != name]
    path.write_text(json.dumps([dataclasses.asdict(r) for r in refs], indent=2))
    path.chmod(0o600)


def revoke_git_gate_provisioned_keys(
    bottle: ManifestBottle, stage_dir: Path, secrets: SecretSource,
) -> None:
    """Revoke every deploy key provisioned for `bottle`, and any granted while
    it ran. Raises on any failure, since a stranded key needs the operator's
    attention."""
    revoke_deploy_keys(deploy_key_refs(bottle) + live_grant_refs(stage_dir), stage_dir,
                       secrets)


def revoke_deploy_keys(
    refs: "tuple[DeployKeyRef, ...]", stage_dir: Path, secrets: SecretSource,
) -> None:
    """Revoke the provisioned deploy key of each of `refs`, once per repo name
    (a repo re-granted while running has a launch ref and a live one for the
    same key). Raises on any failure."""
    seen: set[str] = set()
    for ref in refs:
        id_file = stage_dir / f"{ref.name}-deploy-key-id"
        if ref.name in seen or not id_file.exists():
            continue
        seen.add(ref.name)
        revoke_key(ref, id_file.read_text().strip(), secrets)


def revoke_key(ref: DeployKeyRef, key_id: str, secrets: SecretSource) -> None:
    """Revoke the deploy key `key_id` of `ref`'s repo."""
    from ..deploy_key_provisioner import get_provisioner
    token = secrets.get(ref.forge_token_env)
    if token is None:
        raise MissingEnvVarError(
            ref.forge_token_env,
            f"git-gate.repos[{ref.name!r}] key.forge_token_env"
            f" = {ref.forge_token_env!r}: secret is not set;"
            f" cannot revoke deploy key {key_id}",
        )
    api_url = ref.api_url or f"https://{ref.upstream_host}"
    provisioner = get_provisioner(ref.provider, token, api_url)
    owner_repo = ref.upstream_path
    if owner_repo.endswith(".git"):
        owner_repo = owner_repo[:-4]
    info(f"revoking deploy key {key_id} for git-gate.repos[{ref.name!r}]")
    provisioner.delete(owner_repo, key_id)
    info(f"revoked deploy key {key_id} for git-gate.repos[{ref.name!r}]")


def _resolve_identity_file(
    entry: ManifestGitEntry, slug: str, stage_dir: Path, secrets: SecretSource,
) -> str:
    """The entry's SSH identity file, provisioning a deploy key first for
    a forge kind."""
    if entry.Key.provider in FORGE_KINDS:
        return _provision_dynamic_key(entry, slug, stage_dir, secrets)
    return entry.IdentityFile


def provision_git_gate_dynamic_keys(
    bottle: ManifestBottle,
    plan: "GitGatePlan",
    stage_dir: Path,
    secrets: SecretSource,
) -> "GitGatePlan":
    """Provision the forge keys and return the updated plan. Runs at launch,
    after confirmation, so dry runs and aborted launches create no keys.
    """
    if not plan.upstreams:
        return plan
    _revoke_leftover_keys(bottle, stage_dir, secrets)

    upstreams_by_name: dict[str, GitGateUpstream] = {
        upstream.name: upstream for upstream in plan.upstreams
    }
    updated: list[GitGateUpstream] = []
    for entry in bottle.git:
        upstream = upstreams_by_name.get(entry.Name)
        if upstream is None:
            continue
        if entry.Key.provider in FORGE_KINDS:
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


def _revoke_leftover_keys(
    bottle: ManifestBottle, stage_dir: Path, secrets: SecretSource,
) -> None:
    """Revoke the keys a previous run under this slug left registered. A run
    that was killed never tore down, and the forge refuses a new key with its
    old one's title. Keys last one launch: a resume or a relaunch mints its
    own. A key that can't be revoked is left for the operator, and minting
    then says which collided."""
    refs = deploy_key_refs(bottle) + live_grant_refs(stage_dir)
    stale = [r for r in refs if (stage_dir / f"{r.name}-deploy-key-id").exists()]
    if not stale:
        return
    info("revoking the deploy keys a previous run left registered")
    try:
        revoke_deploy_keys(tuple(stale), stage_dir, secrets)
    except (RuntimeError, MissingEnvVarError) as e:
        warn(f"couldn't revoke a previous run's deploy keys: {e}")
        return
    for ref in stale:
        (stage_dir / f"{ref.name}-deploy-key-id").unlink(missing_ok=True)


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
            "writable": u.writable,
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
            writable=u.get("writable", True),
        ))
    return tuple(upstreams)


__all__ = [
    "DeployKeyRef",
    "deploy_key_refs",
    "forget_live_grant",
    "live_grant_refs",
    "load_upstreams_snapshot",
    "record_live_grant",
    "revoke_deploy_keys",
    "revoke_key",
    "revoke_git_gate_provisioned_keys",
    "provision_git_gate_dynamic_keys",
    "_provision_dynamic_key",
    "_resolve_identity_file",
]
