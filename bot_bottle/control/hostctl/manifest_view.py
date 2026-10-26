"""A running agent's manifest for the console's Manifest tab: the agent file,
each bottle file in its chain as they are now, and what the bottle resolves to
(egress, repos including any granted while it runs, forges, env names).

Env values are never included, only names. The files are the operator's own
and carry secrets by reference (`$label`, `secret: NAME`), not by value.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class ManifestViewError(Exception):
    """The manifest can't be shown; the message says why."""


def _shown(path: Path) -> str:
    home = str(Path.home())
    return "~" + str(path)[len(home):] if str(path).startswith(home + "/") else str(path)


def _live_policy(identity: str) -> str | None:
    """The egress policy the orchestrator holds for the running bottle (the
    bottle's routes, the agent provider's, and any approved since launch;
    credentials by slot name only), or None if it can't be read."""
    # pylint: disable=import-outside-toplevel
    from ...backend.adoption import read as read_adoption
    from ...contract.hostctl import Settings
    from ..client import OrchestratorClient, OrchestratorClientError, discover_orchestrator_url
    adopted = read_adoption(identity)
    if adopted is None:
        return None
    try:
        records = OrchestratorClient(discover_orchestrator_url(Settings())).list_bottles()
    except (OrchestratorClientError, OSError):
        return None
    policy = next((r.get("policy") for r in records
                   if r.get("bottle_id") == adopted.registry_id), None)
    return policy if isinstance(policy, str) else None


def _routes(bottle: Any, policy: str | None) -> tuple[list[dict[str, Any]], bool]:
    """The bottle's egress routes, live from the orchestrator's `policy` when
    it answered (True), else as its files declare them (False)."""
    # pylint: disable-next=import-outside-toplevel
    from ...gateway.egress.schema import load_config
    declared = {route.Host for route in bottle.egress.routes}
    if policy is not None:
        try:
            out = []
            for route in load_config(policy).routes:
                out.append({
                    "host": route.host,
                    "methods": sorted({m for e in route.matches for m in e.methods}),
                    "paths": sorted({p.value for e in route.matches for p in e.paths}),
                    "credential": bool(route.token_env), "git_fetch": route.git_fetch,
                    "declared": route.host in declared})
            return out, True
        except Exception:  # pylint: disable=broad-exception-caught  # fall back to the files
            pass
    return [{"host": r.Host,
             "methods": sorted({m for match in r.Matches for m in match.Methods}),
             "paths": sorted({p.Value for match in r.Matches for p in match.Paths}),
             "credential": False, "git_fetch": False, "declared": True}
            for r in bottle.egress.routes], False


def _repos(bottle: Any, identity: str) -> list[dict[str, Any]]:
    # pylint: disable=import-outside-toplevel
    from ...bottle_state import git_gate_state_dir
    from ...git_gate.provision import live_grant_refs, load_upstreams_snapshot
    repos: dict[str, dict[str, Any]] = {
        e.Name: {"name": e.Name, "url": e.Upstream, "writable": not e.Key.read_only,
                 "live": False}
        for e in bottle.git}
    stage = git_gate_state_dir(identity)
    live = {r.name for r in live_grant_refs(stage)}
    for u in load_upstreams_snapshot(stage):
        if u.name in live:  # granted while it runs, possibly over a launch grant
            repos[u.name] = {"name": u.name, "url": u.upstream_url, "writable": u.writable,
                             "live": True}
    return sorted(repos.values(), key=lambda r: r["name"])


def _approved_routes(identity: str, policy: str | None) -> list[Any]:
    """Routes the live policy has that the bottle didn't launch with: approved
    while it runs."""
    # pylint: disable=import-outside-toplevel
    from ...approvals import _comparable  # pylint: disable=protected-access
    from ...bottle_state import egress_state_dir
    from ...gateway.egress.schema import load_config
    if policy is None:
        return []
    try:
        launch_file = egress_state_dir(identity) / "routes.yaml"
        launched = {r.host: _comparable(r) for r in load_config(launch_file.read_text()).routes}
        live = load_config(policy).routes
    except Exception:  # pylint: disable=broad-exception-caught  # nothing to show
        return []
    return [r for r in live if launched.get(r.host) != _comparable(r)]


def running_bottle(identity: str, policy: str | None) -> dict[str, Any] | None:
    """The bottle "keep" writes to (`approvals.keep_target`), as it would be
    with this run's changes kept: egress approved since launch and repos
    granted while it runs. A route carrying a credential can't be kept, and is
    listed instead. None when the run has nowhere to keep things."""
    # pylint: disable=import-outside-toplevel
    import hashlib
    from ...approvals import bottle_text, keep_target, read_bottle, with_grant, with_routes
    from ...bottle_state import git_gate_state_dir
    from ...git_gate.provision import live_grant_refs
    from .repo_grants import find_forge
    target = keep_target(identity)
    if target is None:
        return None
    original = target.path.read_text(encoding="utf-8") if target.path.is_file() else ""
    fm, body = read_bottle(target.path)
    changes: list[dict[str, str]] = []
    unkept: list[dict[str, str]] = []
    for route in _approved_routes(identity, policy):
        if route.auth_scheme or route.token_env:
            unkept.append({"kind": "egress", "what": route.host,
                           "why": "carries a credential, so it's allowed once only"})
            continue
        after = with_routes(fm, [route])
        if after != fm:
            fm = after
            changes.append({"kind": "egress", "what": route.host})
    for ref in live_grant_refs(git_gate_state_dir(identity)):
        if not ref.grant:
            unkept.append({"kind": "repo", "what": ref.name, "why": "granted before grants "
                           "were recorded; grant it again to keep it"})
            continue
        found = find_forge(identity, ref.grant.split("/", 1)[0])
        after = with_grant(fm, ref.grant, extends=found.bottle if found else "")
        if after != fm:
            fm = after
            changes.append({"kind": "repo", "what": ref.grant})
    updated = bottle_text(fm, body) if changes else original
    return {"target": target.label(), "path": _shown(target.path),
            "project": target.project is not None, "exists": target.path.is_file(),
            "original": original, "updated": updated, "data": fm, "changes": changes,
            "unkept": unkept, "digest": hashlib.sha256(updated.encode()).hexdigest()}


def _load(identity: str) -> tuple[Any, Any]:
    """The bottle's launch record and its agent's manifest as the files are
    now. Raises ManifestViewError."""
    # pylint: disable=import-outside-toplevel
    from ...bottle_state import read_metadata
    from ...manifest.index import ManifestIndex
    from ...manifest.util import ManifestError
    meta = read_metadata(identity)
    if meta is None:
        raise ManifestViewError("this bottle's launch record is missing")
    try:
        index = ManifestIndex.resolve(meta.cwd or str(Path.home()))
        return meta, index.load_for_agent(meta.agent_name, meta.bottle_names or None)
    except ManifestError as e:
        raise ManifestViewError(f"the manifest doesn't load as it is now: {e}") from e


def _live_repo(ref: Any, upstream: Any) -> dict[str, object]:
    """A live grant as a long-form repo, like `manifest.document.repo_document`."""
    key: dict[str, object] = {"provider": "gitea", "forge_token_env": ref.forge_token_env}
    if ref.api_url:
        key["api_url"] = ref.api_url
    if not upstream.writable:
        key["read_only"] = True
    repo: dict[str, object] = {"url": upstream.upstream_url, "key": key}
    if upstream.known_host_key:
        repo["host_key"] = upstream.known_host_key
    return repo


def current_manifest(identity: str, bottle: Any, policy: str | None) -> dict[str, Any]:
    """The agent's latest manifest version, and the manifest it runs with now:
    that version with the supervisor's changes since (egress approved, repos
    granted). A route carrying a credential is allowed once only, so it's
    listed, not added. A bottle launched before versions were kept gets
    version 1 from its files as they are now."""
    # pylint: disable=import-outside-toplevel
    import copy
    import hashlib
    from ... import run_manifest
    from ...approvals import with_routes
    from ...bottle_state import git_gate_state_dir
    from ...git_gate.provision import live_grant_refs, load_upstreams_snapshot
    from ...manifest.document import bottle_document
    last = run_manifest.latest(identity)
    if last is None:
        run_manifest.record(identity, bottle_document(bottle),
                            "the bottle files when first viewed")
        last = run_manifest.latest(identity)
        assert last is not None
    entry, base = last
    doc = copy.deepcopy(base)
    changes: list[dict[str, str]] = []
    unkept: list[dict[str, str]] = []
    for route in _approved_routes(identity, policy):
        if route.auth_scheme or route.token_env:
            unkept.append({"kind": "egress", "what": route.host,
                           "why": "carries a credential, so it's allowed once only"})
            continue
        after = with_routes(doc, [route])
        if after != doc:
            doc = after
            changes.append({"kind": "egress", "what": route.host})
    stage = git_gate_state_dir(identity)
    upstreams = {u.name: u for u in load_upstreams_snapshot(stage)}
    for ref in live_grant_refs(stage):
        upstream = upstreams.get(ref.name)
        if upstream is None:
            continue
        raw_git = doc.get("git-gate")
        git: dict[str, object] = dict(raw_git) if isinstance(raw_git, dict) else {}
        raw_repos = git.get("repos")
        repos: dict[str, object] = dict(raw_repos) if isinstance(raw_repos, dict) else {}
        repo = _live_repo(ref, upstream)
        if repos.get(ref.name) != repo:
            repos[ref.name] = repo
            git["repos"] = repos
            doc["git-gate"] = git
            access = "write" if upstream.writable else "read-only"
            changes.append({"kind": "repo", "what": f"{ref.name} ({access})"})
    text = run_manifest.document_text(doc)
    return {"version": entry["version"], "saved_at": entry.get("at", ""),
            "message": entry.get("message", ""), "versions": len(run_manifest.versions(identity)),
            "base": run_manifest.document_text(base), "text": text, "data": doc,
            "changes": changes, "unkept": unkept,
            "digest": hashlib.sha256(text.encode()).hexdigest()}


def save_manifest(identity: str, digest: str, *, also_bottle: bool) -> dict[str, Any]:
    """Save the manifest the agent runs with now as its next version, if it's
    still what the console showed (`digest`); with `also_bottle`, also keep
    the run's changes in its bottle file. Raises ManifestViewError."""
    # pylint: disable=import-outside-toplevel
    from ... import run_manifest
    from ...approvals import keep_target, save_bottle
    _, manifest = _load(identity)
    policy = _live_policy(identity)
    current = current_manifest(identity, manifest.bottle, policy)
    if current["digest"] != digest:
        raise ManifestViewError("the manifest changed since it was shown; refresh and try again")
    if not current["changes"]:
        raise ManifestViewError("there's nothing new to save")
    whats = ", ".join(c["what"] for c in current["changes"])
    saved: dict[str, Any] = {"version": run_manifest.record(identity, current["data"],
                                                            f"saved: {whats}")}
    if also_bottle:
        running = running_bottle(identity, policy)
        target = keep_target(identity)
        if running is None or target is None:
            saved["bottle_error"] = "this bottle has no file to keep changes in"
        elif running["changes"]:
            save_bottle(target, running["updated"], f"keep from the console: {whats}")
            saved["bottle"] = running["target"]
    return saved


_BOTTLE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def save_as_bottle(identity: str, digest: str, name: str) -> dict[str, Any]:
    """Write the manifest the agent runs with now as a new host bottle `name`
    (standalone: the whole chain flattened), if it's still what the console
    showed, and record it as the agent's next version. Raises
    ManifestViewError."""
    # pylint: disable=import-outside-toplevel
    from ... import run_manifest
    from ...approvals import KeepTarget, bottle_text, save_bottle
    from ...paths import bot_bottle_root
    if not _BOTTLE_NAME.match(name) or name.endswith(".md"):
        raise ManifestViewError("a bottle name is letters, digits, '.', '_' and '-'")
    path = bot_bottle_root() / "bottles" / f"{name}.md"
    if path.exists():
        raise ManifestViewError(f"there's already a bottle named {name}")
    meta, manifest = _load(identity)
    current = current_manifest(identity, manifest.bottle, _live_policy(identity))
    if current["digest"] != digest:
        raise ManifestViewError("the manifest changed since it was shown; refresh and try again")
    body = (f"Saved from {meta.agent_name}'s run ({identity}) in the console: everything it "
            f"ran with, its bottles flattened into one.")
    save_bottle(KeepTarget(path, None), bottle_text(current["data"], body),
                f"save {identity}'s manifest as {name}")
    return {"version": run_manifest.record(identity, current["data"], f"saved as bottle {name}"),
            "new_bottle": name}


def agent_manifest(identity: str) -> dict[str, Any]:
    """The manifest of the bottle `identity` runs: its current version and
    changes (`current`), where they could also be kept (`bottle_file`), and a
    summary of what it allows (`resolved`). Raises ManifestViewError."""
    meta, manifest = _load(identity)
    bottle = manifest.bottle
    policy = _live_policy(identity)
    routes, live = _routes(bottle, policy)
    running = running_bottle(identity, policy)
    names = meta.bottle_names or ((manifest.agent.bottle,) if manifest.agent.bottle else ())
    return {
        "current": current_manifest(identity, bottle, policy),
        "bottle_file": None if running is None else {
            "target": running["target"], "path": running["path"],
            "project": running["project"], "changes": running["changes"]},
        "agent": meta.agent_name,
        "bottles": list(names),
        "harness": manifest.agent.agent_provider.harness,
        "resolved": {
            "egress": routes,
            "egress_live": live,
            "repos": _repos(bottle, identity),
            "forges": [{"name": f.name, "url": f.url} for f in bottle.forges],
            "env": sorted(bottle.env),
            "supervise": bottle.supervise,
            "nested_containers": bottle.nested_containers,
        },
    }


__all__ = ["ManifestViewError", "agent_manifest", "current_manifest", "running_bottle",
           "save_as_bottle", "save_manifest"]
