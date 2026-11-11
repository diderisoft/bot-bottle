"""Granting a repo to a bottle that's already running (PRD 0105): mint its
deploy key, add it to the bottle's upstreams (which re-provisioning the
gateway reads), and point the agent's git at the gate for it.

The refs of keys minted this way are rows of the host DB's
`git_gate_live_grants` (`state`), so teardown revokes them with the launch's
own.
"""

from __future__ import annotations

import dataclasses
import re
import shlex
from pathlib import Path
from urllib.parse import urlparse
from typing import TYPE_CHECKING, Any

from ..contract.forges import FORGE_KINDS
from ..contract.identity import GIT_HTTP_PORT, IDENTITY_HEADER
from ..control.git_gate_render import (
    GitGateUpstream, git_gate_known_hosts_line, git_gate_upstreams_for_bottle,
)
from ..manifest import ManifestBottle
from ..manifest.forges import ManifestForge, parse_grant, split_grant_alias
from ..manifest.git import ManifestGitEntry
from ..secret_source import SecretSource
from . import state as _state
from .provision import (
    DeployKeyRef, _provision_dynamic_key, forget_live_grant, live_grant_refs,
    record_live_grant, revoke_key, saved_upstreams,
)

if TYPE_CHECKING:
    from ..backend.base import Bottle

class GrantError(Exception):
    """A repo couldn't be granted to the running bottle; the message says why."""


def grant_entry(grant: str, forge: ManifestForge) -> ManifestGitEntry:
    """`<forge>/<owner>/<repo>[:write]` on `forge` as a repo entry, the way a
    grant in a bottle file expands (`manifest.forges.expand_grants`).
    Supports `<alias>=<grant>` to mount the repo under a different local name,
    letting two repos with the same upstream repo name coexist."""
    alias, bare_grant = split_grant_alias(grant)
    _, owner, repo, writable = parse_grant(bare_grant, "the grant")
    raw: dict[str, object] = {"url": f"{forge.ssh}/{owner}/{repo}.git",
                              "key": forge.key(writable)}
    if forge.host_key:
        raw["host_key"] = forge.host_key
    return ManifestGitEntry.from_repos_entry("(live grant)", alias or repo, raw)


def name_taken(entry: ManifestGitEntry, identity: str) -> set[str] | None:
    """The bottled agent `identity`'s repo names when a different repo
    already has `entry`'s (a grant then needs an alias), else None."""
    upstreams = saved_upstreams(identity)
    current = next((u for u in upstreams if u.name == entry.Name), None)
    if current is None:
        return None
    (upstream,) = git_gate_upstreams_for_bottle(ManifestBottle(git=(entry,)))
    if current.upstream_url in (entry.Upstream, upstream.upstream_url):
        return None
    return {u.name for u in upstreams}


def provision_live(entry: ManifestGitEntry, *, slug: str, stage_dir: Path,
                   secrets: SecretSource, grant: str = "") -> GitGateUpstream:
    """Mint `entry`'s deploy key and add it to the upstreams of the bottled
    agent `slug`. The ref is recorded before minting, so a key can't outlive
    the bottle unrevoked. Re-provisioning the gateway then picks the repo up.

    Granting a repo the bottle has with other access (read-only to write, or
    back) replaces it: the new key is minted, then the old one revoked."""
    upstreams = saved_upstreams(slug)
    current = next((u for u in upstreams if u.name == entry.Name), None)
    writable = not entry.Key.read_only
    # The bottle's protect_default_branch, as its other repos have it.
    protect = any(u.protect_default_branch for u in upstreams)
    (upstream,) = git_gate_upstreams_for_bottle(
        ManifestBottle(git=(entry,), git_protect_default_branch=protect))
    if current is not None and current.upstream_url not in (entry.Upstream,
                                                            upstream.upstream_url):
        raise GrantError(f"the bottle already has a different repo named {entry.Name!r}")
    ref = DeployKeyRef(
        name=entry.Name, forge_token_env=entry.Key.forge_token_env, api_url=entry.Key.api_url,
        upstream_host=entry.UpstreamHost, upstream_path=entry.UpstreamPath, grant=grant,
        provider=entry.Key.provider)
    if current is not None and current.writable == writable:
        # Already granted with this access: nothing to mint; just record the
        # grant, so it can be kept.
        if grant and any(r.name == entry.Name for r in live_grant_refs(slug)):
            record_live_grant(slug, ref)
            return current
        raise GrantError(f"the bottle already has {entry.Name} "
                         f"({'write' if writable else 'read-only'})")
    if entry.Key.provider not in FORGE_KINDS:
        raise GrantError("only forge grants can be added to a running bottle")
    stage_dir.mkdir(parents=True, exist_ok=True)
    if upstream.transport == "https":
        # Over HTTPS with the user's token (ADR 0014): no key to mint or revoke.
        record_live_grant(slug, ref)
        _state.store().put_upstream(slug, upstream)
        return upstream
    if not entry.KnownHostKey:
        raise GrantError(f"the forge at {entry.UpstreamHost} has no SSH host key pinned; "
                         f"connect it again with an SSH address this host can reach")
    if upstream.known_host_key:
        known_hosts = stage_dir / f"{entry.Name}-known_hosts"
        known_hosts.write_text(git_gate_known_hosts_line(
            upstream.upstream_host, upstream.upstream_port, upstream.known_host_key))
        known_hosts.chmod(0o600)
        upstream = dataclasses.replace(upstream, known_hosts_file=known_hosts)
    id_file = stage_dir / f"{entry.Name}-deploy-key-id"
    old_key = id_file.read_text().strip() if current is not None and id_file.is_file() else ""
    record_live_grant(slug, ref)
    suffix = "" if current is None else (":write" if writable else ":read")
    identity_file = _provision_dynamic_key(entry, slug, stage_dir, secrets, title_suffix=suffix)
    upstream = dataclasses.replace(upstream, identity_file=identity_file)
    _state.store().put_upstream(slug, upstream)
    if old_key:
        revoke_key(ref, old_key, secrets)
    return upstream


# The agent processes' proxy URL carries the identity token as its password
# (`http://bottle:<token>@<gateway>:<port>`), on every backend.
_PROXY_PROBE = ("for f in /proc/[0-9]*/environ; do "
                "tr '\\0' '\\n' < \"$f\" 2>/dev/null | grep -m1 '^HTTPS_PROXY=http://bottle:' "
                "&& break; done")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]+$")


def _gate_from_proxy(bottle: "Bottle") -> str:
    """For a bottle launched before launch always wrote the gate's identity
    header: write it from the agent's proxy URL, whose host is the gate's on
    every backend. Returns the gate URL, or "". The token is never put in an
    error or a log."""
    found = bottle.exec(_PROXY_PROBE)  # as the agent: root may not read its env
    url = urlparse((found.stdout or "").strip().partition("=")[2])
    token, host = url.password or "", url.hostname or ""
    if not (token and host and _TOKEN_RE.match(token)):
        return ""
    gate = f"http://{host}:{GIT_HTTP_PORT}/"
    wrote = bottle.exec(f"git config --global {shlex.quote(f'http.{gate}.extraHeader')} "
                        f"{shlex.quote(f'{IDENTITY_HEADER}: {token}')}")
    return gate if wrote.returncode == 0 else ""


def point_agent_at_gate(bottle: "Bottle", entry: ManifestGitEntry) -> None:
    """Add the gate's `insteadOf` rewrite for `entry` to the agent's git
    config, next to the identity header (written first if the bottle predates
    launch always writing it)."""
    found = bottle.exec("git config --global --get-regexp '^http\\..*\\.extraheader$'")
    key = (found.stdout or "").split(" ", 1)[0] if found.returncode == 0 else ""
    gate = key.removeprefix("http.").removesuffix(".extraheader")
    # A header with no host (`http:///`) names no gate: find it from the proxy.
    if not gate.startswith("http://") or not urlparse(gate).netloc:
        gate = _gate_from_proxy(bottle)
    if not gate:
        raise GrantError("couldn't find the agent's git-gate address; relaunch the bottle")
    rewrite = f"url.{gate.removesuffix('/')}/{entry.Name}.git.insteadOf"
    have = bottle.exec(f"git config --global --get-all {shlex.quote(rewrite)}")
    if entry.Upstream in (have.stdout or "").split("\n"):
        return  # re-granted with other access: the rewrite is already there
    added = bottle.exec(
        f"git config --global --add {shlex.quote(rewrite)} {shlex.quote(entry.Upstream)}")
    if added.returncode != 0:
        raise GrantError(f"couldn't update the agent's git config: "
                         f"{(added.stderr or added.stdout or '').strip()}")


def revoke_live(name: str, *, identity: str, stage_dir: Path, secrets: SecretSource,
                transport: Any, registry_id: str, bottle: "Bottle",
                launched: tuple[str, ...]) -> None:
    """Take the repo `name`, granted while the bottled agent `identity` runs,
    away again: revoke its deploy key, drop it from its upstreams and the
    gateway, and remove the agent's rewrite for it. A repo the bottle launched
    with (`launched`) stays: its launch key isn't this run's to revoke."""
    ref = next((r for r in live_grant_refs(identity) if r.name == name), None)
    if ref is None:
        raise GrantError(f"{name} wasn't granted while the bottle runs")
    if name in launched:
        raise GrantError(f"{name} came with the bottle at launch; it can't be removed "
                         f"while it runs")
    id_file = stage_dir / f"{name}-deploy-key-id"
    if id_file.is_file():
        revoke_key(ref, id_file.read_text().strip(), secrets)
    _state.store().drop_upstream(identity, name)
    forget_live_grant(identity, name)
    for suffix in ("-key", "-deploy-key-id", "-known_hosts"):
        (stage_dir / f"{name}{suffix}").unlink(missing_ok=True)
    transport.exec(["rm", "-rf", f"/git/{registry_id}/{name}.git",
                    f"/git-gate/creds/{registry_id}/{name}-key",
                    f"/git-gate/creds/{registry_id}/{name}-known_hosts"])
    found = bottle.exec("git config --global --get-regexp '^url\\..*\\.insteadof$'")
    for line in (found.stdout or "").splitlines():
        key = line.split(" ", 1)[0]
        if key.lower().endswith(f"/{name}.git.insteadof".lower()):
            bottle.exec(f"git config --global --unset-all {shlex.quote(key)}")


__all__ = ["GrantError", "grant_entry", "point_agent_at_gate", "provision_live", "revoke_live"]
