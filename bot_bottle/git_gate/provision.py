"""git-gate deploy keys for forge (`gitea`, `github`) upstreams: minted
through the forge's API at launch and revoked at teardown. The forge client is imported lazily."""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING

from ..bottle_state import globalize_slug
from ..contract.forges import FORGE_KINDS
from ..errors import MissingEnvVarError
from ..lib.log import info, warn
from ..manifest import ManifestBottle, ManifestGitEntry
from ..manifest.git import ManifestKeyConfig, git_transport
from ..secret_source import SecretSource
from ..control.git_gate_render import GitGateUpstream
from . import state as _state

if TYPE_CHECKING:
    from .plan import GitGatePlan

def _forge_token(provider: str, name: str, secrets: SecretSource) -> str | None:
    """The forge token in secret `name`; a GitHub user access token is renewed
    first when it's about to expire (`contrib/github/user_token.py`)."""
    if provider == "github":
        # pylint: disable-next=import-outside-toplevel
        from ..contrib.github.user_token import current_token
        return current_token(name, secrets)
    return secrets.get(name)


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
    token = _forge_token(pk.provider, pk.forge_token_env, secrets)
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
    with _token_alert(pk.provider, pk.forge_token_env, entry.UpstreamHost):
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
    keep in the host DB."""

    name: str
    forge_token_env: str
    api_url: str
    upstream_host: str
    upstream_path: str
    # A live grant's `<forge>/<owner>/<repo>[:write]`, so it can be kept.
    grant: str = ""
    # The forge kind that minted it; records from before GitHub are Gitea's.
    provider: str = "gitea"


def mints_key(key: ManifestKeyConfig) -> bool:
    """Whether a repo's key is a deploy key minted per launch: a forge kind
    reached over SSH (a GitHub repo over HTTPS uses the user's token)."""
    return key.provider in FORGE_KINDS and git_transport(key) == "ssh"


def deploy_key_refs(bottle: ManifestBottle) -> tuple[DeployKeyRef, ...]:
    """The bottle's repos whose deploy keys are minted per launch."""
    return tuple(
        DeployKeyRef(name=e.Name, forge_token_env=e.Key.forge_token_env,
                     api_url=e.Key.api_url, upstream_host=e.UpstreamHost,
                     upstream_path=e.UpstreamPath, provider=e.Key.provider)
        for e in bottle.git if mints_key(e.Key)
    )


def live_grant_refs(identity: str) -> tuple[DeployKeyRef, ...]:
    """The deploy keys granted to the running bottled agent `identity` after
    launch (`live_grant`), revoked at teardown with the launch's own."""
    return tuple(DeployKeyRef(**g) for g in _state.store().live_grants(identity))


def record_live_grant(identity: str, ref: DeployKeyRef) -> None:
    """Record `ref`, replacing one for the same repo name (a re-grant)."""
    grant = dataclasses.asdict(ref)
    _state.store().record_live_grant(identity, {k: str(grant[k])
                                                for k in ("name", *_state.GRANT_FIELDS)})


def forget_live_grant(identity: str, name: str) -> None:
    """Drop the live grant of the repo `name` from the record."""
    _state.store().forget_live_grant(identity, name)


def revoke_git_gate_provisioned_keys(
    bottle: ManifestBottle, identity: str, stage_dir: Path, secrets: SecretSource,
) -> None:
    """Revoke every deploy key provisioned for `bottle`, and any granted while
    it ran. Raises on any failure, since a stranded key needs the operator's
    attention."""
    revoke_deploy_keys(deploy_key_refs(bottle) + live_grant_refs(identity), stage_dir,
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


@contextlib.contextmanager
def _token_alert(provider: str, secret: str, forge: str) -> Generator[None]:
    """A Gitea token the forge refuses raises a console alert (`forge_alerts`)
    until a call with it works again; GitHub deploy keys use the user's token,
    whose alerts come from its renewal (`github_git_tokens`)."""
    # pylint: disable=import-outside-toplevel
    from .. import forge_alerts
    from ..deploy_key_provisioner import ForgeTokenRefusedError
    try:
        yield
    except ForgeTokenRefusedError as e:
        if provider == "gitea":
            forge_alerts.raise_alert(secret, kind=forge_alerts.GITEA_TOKEN_REFUSED,
                                     forge=forge, message=str(e))
        raise
    if provider == "gitea":
        forge_alerts.clear(secret)


def revoke_key(ref: DeployKeyRef, key_id: str, secrets: SecretSource) -> None:
    """Revoke the deploy key `key_id` of `ref`'s repo."""
    from ..deploy_key_provisioner import get_provisioner
    token = _forge_token(ref.provider, ref.forge_token_env, secrets)
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
    with _token_alert(ref.provider, ref.forge_token_env, ref.upstream_host):
        provisioner.delete(owner_repo, key_id)
    info(f"revoked deploy key {key_id} for git-gate.repos[{ref.name!r}]")


def _resolve_identity_file(
    entry: ManifestGitEntry, slug: str, stage_dir: Path, secrets: SecretSource,
) -> str:
    """The entry's SSH identity file, provisioning a deploy key first for
    a forge kind."""
    if mints_key(entry.Key):
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
    _revoke_leftover_keys(bottle, plan.slug, stage_dir, secrets)

    upstreams_by_name: dict[str, GitGateUpstream] = {
        upstream.name: upstream for upstream in plan.upstreams
    }
    updated: list[GitGateUpstream] = []
    for entry in bottle.git:
        upstream = upstreams_by_name.get(entry.Name)
        if upstream is None:
            continue
        if mints_key(entry.Key):
            identity_file = _provision_dynamic_key(entry, plan.slug, stage_dir, secrets)
            upstream = dataclasses.replace(upstream, identity_file=identity_file)
        updated.append(upstream)

    if len(updated) != len(plan.upstreams):
        updated_names = {u.name for u in updated}
        for upstream in plan.upstreams:
            if upstream.name not in updated_names:
                updated.append(upstream)

    final_plan = dataclasses.replace(plan, upstreams=tuple(updated))
    save_upstreams(plan.slug, final_plan.upstreams)
    return final_plan


def _revoke_leftover_keys(
    bottle: ManifestBottle, identity: str, stage_dir: Path, secrets: SecretSource,
) -> None:
    """Revoke the keys a previous run under this slug left registered. A run
    that was killed never tore down, and the forge refuses a new key with its
    old one's title. Keys last one launch: a resume or a relaunch mints its
    own. A key that can't be revoked is left for the operator, and minting
    then says which collided."""
    refs = deploy_key_refs(bottle) + live_grant_refs(identity)
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


def save_upstreams(identity: str, upstreams: tuple[GitGateUpstream, ...]) -> None:
    """Keep the resolved upstream table of the bottled agent `identity`, so
    re-provisioning its gateway doesn't need the manifest."""
    _state.store().set_upstreams(identity, upstreams)


def saved_upstreams(identity: str) -> tuple[GitGateUpstream, ...]:
    """The upstreams `save_upstreams` kept (with those granted or taken away
    since), or () without upstreams."""
    return _state.store().upstreams(identity)


__all__ = [
    "DeployKeyRef",
    "deploy_key_refs",
    "forget_live_grant",
    "live_grant_refs",
    "mints_key",
    "record_live_grant",
    "revoke_deploy_keys",
    "revoke_key",
    "revoke_git_gate_provisioned_keys",
    "provision_git_gate_dynamic_keys",
    "save_upstreams",
    "saved_upstreams",
    "_provision_dynamic_key",
    "_resolve_identity_file",
]
