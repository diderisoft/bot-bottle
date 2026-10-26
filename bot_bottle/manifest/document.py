"""A resolved bottle as one bottle document: its whole `extends` chain
flattened into the frontmatter of a single bottle file that reads back as the
same bottle (PRD 0107). Used for a bottled agent's manifest versions.

Repos are written in long form, so the document stands alone; the forges and
the credentials they take their token from are kept, so a grant against them
still reads. Secrets appear by name only, as in any bottle file.
"""

from __future__ import annotations

from .bottle import ManifestBottle
from .egress import ManifestEgressRoute, ManifestMatchEntry
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
    if route.AuthScheme:
        inspect["auth"] = {"scheme": route.AuthScheme, "token_ref": route.TokenRef}
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
    out: dict[str, object] = {"url": entry.Upstream, "key": key}
    if entry.KnownHostKey:
        out["host_key"] = entry.KnownHostKey
    return out


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
    if bottle.forges:
        forges: dict[str, object] = {}
        for forge in bottle.forges:
            label = f"{forge.name.replace('@', '-at-').replace('.', '-').replace(':', '-')}-token"
            credentials[label] = {"source": "secret", "secret": forge.token_secret}
            entry: dict[str, object] = {"kind": forge.kind, "url": forge.url, "ssh": forge.ssh,
                                        "token": f"${label}"}
            if forge.account:
                entry["account"] = forge.account
            if forge.host_key:
                entry["host_key"] = forge.host_key
            forges[forge.name] = entry
        git["forges"] = forges
    if bottle.git:
        git["repos"] = {e.Name: repo_document(e) for e in bottle.git}
    if credentials:
        doc["credentials"] = credentials
    if git:
        doc["git-gate"] = git
    egress: dict[str, object] = {}
    if bottle.egress.routes:
        egress["routes"] = [route_document(r) for r in bottle.egress.routes]
    if bottle.egress.Log:
        egress["log"] = bottle.egress.Log
    if egress:
        doc["egress"] = egress
    doc["supervise"] = bottle.supervise
    doc["nested_containers"] = bottle.nested_containers
    audit = {k: v for k, v in (("delivery", bottle.audit.delivery),
                               ("firehose", bottle.audit.firehose)) if v}
    if audit:
        doc["audit"] = audit
    return doc


__all__ = ["bottle_document", "repo_document", "route_document"]
