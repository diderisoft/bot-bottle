"""A resolved bottle as one bottle document: its whole `extends` chain
flattened into the frontmatter of a single bottle file that reads back as the
same bottle (PRD 0107). Used for a bottled agent's manifest versions.

A repo granted on one of the chain's forges is written as its grant
(`<forge>/<owner>/<repo>[:write]`), others in long form; the forges and the
credentials they take their token from are kept, so the document stands alone.
Secrets appear by name only, as in any bottle file.
"""

from __future__ import annotations

from .bottle import ManifestBottle
from .egress import ManifestEgressRoute, ManifestMatchEntry
from .forges import ManifestForge
from .git import ManifestGitEntry


def _match(entry: ManifestMatchEntry) -> dict[str, object]:
    out: dict[str, object] = {}
    if entry.Paths:
        out["paths"] = [{"type": p.Type, "value": p.Value} for p in entry.Paths]
    if entry.Methods:
        out["methods"] = list(entry.Methods)
    if entry.Headers:
        out["headers"] = [{"name": h.Name, "value": h.Value, "type": h.Type}
                          for h in entry.Headers]
    return out


def _detectors(value: tuple[str, ...] | None) -> object:
    return False if value == () else list(value or ())


def route_document(route: ManifestEgressRoute) -> dict[str, object]:
    """One egress route as a bottle file declares it."""
    out: dict[str, object] = {"host": route.Host}
    if not route.Inspect:
        out["inspect"] = False
        return out
    inspect: dict[str, object] = {}
    if route.Matches:
        inspect["matches"] = [_match(m) for m in route.Matches]
    if route.AuthScheme or route.AuthHeader:
        auth: dict[str, object] = {}
        if route.AuthScheme:
            auth["scheme"] = route.AuthScheme
        if route.AuthHeader:
            auth["header"] = route.AuthHeader
        inspect["auth"] = {**auth, "token_ref": route.TokenRef}
    if route.GitFetch:
        inspect["git"] = {"fetch": True}
    if route.PreserveAuth:
        inspect["preserve_auth"] = True
    if route.OutboundDetectors is not None:
        inspect["outbound_detectors"] = _detectors(route.OutboundDetectors)
    if route.InboundDetectors is not None:
        inspect["inbound_detectors"] = _detectors(route.InboundDetectors)
    if route.OutboundOnMatch:
        inspect["outbound_on_match"] = route.OutboundOnMatch
    if inspect:
        out["inspect"] = inspect
    return out


def repo_document(entry: ManifestGitEntry) -> dict[str, object]:
    """One git-gate repo in long form."""
    key: dict[str, object] = {"provider": entry.Key.provider}
    if entry.Key.provider == "static":
        key["path"] = entry.Key.path
    else:
        key["forge_token_env"] = entry.Key.forge_token_env
        if entry.Key.api_url:
            key["api_url"] = entry.Key.api_url
        if entry.Key.read_only:
            key["read_only"] = True
        if entry.Key.transport:
            key["transport"] = entry.Key.transport
    out: dict[str, object] = {"url": entry.Upstream, "key": key}
    if entry.KnownHostKey:
        out["host_key"] = entry.KnownHostKey
    return out


def grant_for(entry: ManifestGitEntry, forges: tuple[ManifestForge, ...] | list[ManifestForge],
              ) -> str:
    """The grant `entry` was expanded from (`manifest.forges.expand_grants`),
    if it matches one of `forges`, else ""."""
    for forge in forges:
        prefix = f"{forge.ssh}/"
        if not (entry.Upstream.startswith(prefix) and entry.Upstream.endswith(".git")):
            continue
        if (entry.Key.provider, entry.Key.forge_token_env, entry.Key.api_url,
                entry.Key.transport) != (forge.kind, forge.token_secret, forge.url,
                                         forge.transport):
            continue
        if (entry.KnownHostKey or "") != forge.host_key:
            continue
        path = entry.Upstream[len(prefix):-len(".git")]
        if path.count("/") != 1:
            continue
        return f"{forge.name}/{path}" + ("" if entry.Key.read_only else ":write")
    return ""


def forge_document(forge: ManifestForge) -> tuple[str, dict[str, object], dict[str, object]]:
    """One forge as a bottle file declares it: its credential's label, the
    credential, and the `git-gate.forges` entry."""
    label = f"{forge.name.replace('@', '-at-').replace('.', '-').replace(':', '-')}-token"
    entry: dict[str, object] = {"kind": forge.kind, "url": forge.url, "ssh": forge.ssh,
                                "token": f"${label}"}
    if forge.account:
        entry["account"] = forge.account
    if forge.host_key:
        entry["host_key"] = forge.host_key
    return label, {"source": "secret", "secret": forge.token_secret}, entry


def bottle_document(bottle: ManifestBottle) -> dict[str, object]:
    """`bottle` as one bottle file's frontmatter, without `extends`."""
    doc: dict[str, object] = {}
    if bottle.env:
        doc["env"] = dict(bottle.env)
    credentials: dict[str, object] = {}
    git: dict[str, object] = {}
    user = {k: v for k, v in (("name", bottle.git_user.name),
                              ("email", bottle.git_user.email)) if v}
    if user:
        git["user"] = user
    if bottle.git_trailers:
        git["trailers"] = bottle.git_trailers
    if bottle.git_protect_default_branch is not None:
        git["protect_default_branch"] = bottle.git_protect_default_branch
    if bottle.forges:
        forges: dict[str, object] = {}
        for forge in bottle.forges:
            label, credentials[label], forges[forge.name] = forge_document(forge)
        git["forges"] = forges
    if bottle.git:
        git["repos"] = {e.Name: grant_for(e, bottle.forges) or repo_document(e)
                        for e in bottle.git}
    if credentials:
        doc["credentials"] = credentials
    if git:
        doc["git-gate"] = git
    egress: dict[str, object] = {}
    if bottle.egress.routes or bottle.egress.Blocked:
        egress["routes"] = [route_document(r) for r in bottle.egress.routes] + [
            {"host": h, "blocked": True} for h in bottle.egress.Blocked]
    if bottle.egress.Log:
        egress["log"] = bottle.egress.Log
    if egress:
        doc["egress"] = egress
    doc["supervise"] = bottle.supervise
    doc["nested_containers"] = bottle.nested_containers
    if bottle.preserve_container:  # only when set, so other bottles' documents don't change
        doc["preserve_container"] = True
    audit = {k: v for k, v in (("delivery", bottle.audit.delivery),
                               ("firehose", bottle.audit.firehose)) if v}
    if audit:
        doc["audit"] = audit
    if resources := bottle.resources.document():  # only when set, like preserve_container
        doc["resources"] = resources
    return doc


__all__ = ["bottle_document", "forge_document", "grant_for", "repo_document", "route_document"]
