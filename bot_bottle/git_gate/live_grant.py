"""Granting a repo to a bottle that's already running (PRD 0105): mint its
deploy key, add it to the bottle's upstream snapshot (which re-provisioning the
gateway reads), and point the agent's git at the gate for it.

The refs of keys minted this way are kept in the bottle's git-gate state dir
(`live-grants.json`), so teardown revokes them with the launch's own.
"""

from __future__ import annotations

import dataclasses
import re
import shlex
from pathlib import Path
from urllib.parse import urlparse
from typing import TYPE_CHECKING

from ..contract.identity import GIT_HTTP_PORT, IDENTITY_HEADER
from ..control.git_gate_render import (
    GitGateUpstream, git_gate_known_hosts_line, git_gate_upstreams_for_bottle,
)
from ..manifest import ManifestBottle
from ..manifest.forges import ManifestForge, parse_grant
from ..manifest.git import ManifestGitEntry
from ..secret_source import SecretSource
from .provision import (
    DeployKeyRef, _provision_dynamic_key, _write_upstreams_snapshot, live_grant_refs,
    load_upstreams_snapshot, record_live_grant, revoke_key,
)

if TYPE_CHECKING:
    from ..backend.base import Bottle

class GrantError(Exception):
    """A repo couldn't be granted to the running bottle; the message says why."""


def grant_entry(grant: str, forge: ManifestForge) -> ManifestGitEntry:
    """`<forge>/<owner>/<repo>[:write]` on `forge` as a repo entry, the way a
    grant in a bottle file expands (`manifest.forges.expand_grants`)."""
    _, owner, repo, writable = parse_grant(grant, "the grant")
    raw: dict[str, object] = {
        "url": f"{forge.ssh}/{owner}/{repo}.git",
        "key": {"provider": forge.kind, "forge_token_env": forge.token_secret,
                "api_url": forge.url, "read_only": not writable},
    }
    if forge.host_key:
        raw["host_key"] = forge.host_key
    return ManifestGitEntry.from_repos_entry("(live grant)", repo, raw)


def provision_live(entry: ManifestGitEntry, *, slug: str, stage_dir: Path,
                   secrets: SecretSource, grant: str = "") -> GitGateUpstream:
    """Mint `entry`'s deploy key and add it to the bottle's upstream snapshot.
    The ref is recorded before minting, so a key can't outlive the bottle
    unrevoked. Re-provisioning the gateway then picks the repo up.

    Granting a repo the bottle has with other access (read-only to write, or
    back) replaces it: the new key is minted, then the old one revoked."""
    upstreams = load_upstreams_snapshot(stage_dir)
    current = next((u for u in upstreams if u.name == entry.Name), None)
    writable = not entry.Key.read_only
    if current is not None and current.upstream_url != entry.Upstream:
        raise GrantError(f"the bottle already has a different repo named {entry.Name!r}")
    ref = DeployKeyRef(
        name=entry.Name, forge_token_env=entry.Key.forge_token_env, api_url=entry.Key.api_url,
        upstream_host=entry.UpstreamHost, upstream_path=entry.UpstreamPath, grant=grant)
    if current is not None and current.writable == writable:
        # Already granted with this access: nothing to mint; just record the
        # grant, so it can be kept.
        if grant and any(r.name == entry.Name for r in live_grant_refs(stage_dir)):
            record_live_grant(stage_dir, ref)
            return current
        raise GrantError(f"the bottle already has {entry.Name} "
                         f"({'write' if writable else 'read-only'})")
    if entry.Key.provider != "gitea":
        raise GrantError("only forge (gitea) grants can be added to a running bottle")
    if not entry.KnownHostKey:
        raise GrantError(f"the forge at {entry.UpstreamHost} has no SSH host key pinned; "
                         f"connect it again with an SSH address this host can reach")
    stage_dir.mkdir(parents=True, exist_ok=True)
    (upstream,) = git_gate_upstreams_for_bottle(ManifestBottle(git=(entry,)))
    if upstream.known_host_key:
        known_hosts = stage_dir / f"{entry.Name}-known_hosts"
        known_hosts.write_text(git_gate_known_hosts_line(
            upstream.upstream_host, upstream.upstream_port, upstream.known_host_key))
        known_hosts.chmod(0o600)
        upstream = dataclasses.replace(upstream, known_hosts_file=known_hosts)
    id_file = stage_dir / f"{entry.Name}-deploy-key-id"
    old_key = id_file.read_text().strip() if current is not None and id_file.is_file() else ""
    record_live_grant(stage_dir, ref)
    suffix = "" if current is None else (":write" if writable else ":read")
    identity_file = _provision_dynamic_key(entry, slug, stage_dir, secrets, title_suffix=suffix)
    upstream = dataclasses.replace(upstream, identity_file=identity_file)
    _write_upstreams_snapshot(
        stage_dir, (*(u for u in upstreams if u.name != entry.Name), upstream))
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
    if not gate.startswith("http://"):
        gate = _gate_from_proxy(bottle)
    if not gate:
        raise GrantError("couldn't find the agent's git-gate address; relaunch the bottle")
    rewrite = f"url.{gate.rstrip('/')}/{entry.Name}.git.insteadOf"
    have = bottle.exec(f"git config --global --get-all {shlex.quote(rewrite)}")
    if entry.Upstream in (have.stdout or "").split("\n"):
        return  # re-granted with other access: the rewrite is already there
    added = bottle.exec(
        f"git config --global --add {shlex.quote(rewrite)} {shlex.quote(entry.Upstream)}")
    if added.returncode != 0:
        raise GrantError(f"couldn't update the agent's git config: "
                         f"{(added.stderr or added.stdout or '').strip()}")


__all__ = ["GrantError", "grant_entry", "point_agent_at_gate", "provision_live"]
