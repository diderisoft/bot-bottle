"""A running agent's manifest for the console's Manifest tab: the agent file,
each bottle file in its chain as they are now, and what the bottle resolves to
(egress, repos including any granted while it runs, forges, env names).

Env values are never included, only names. The files are the operator's own
and carry secrets by reference (`$label`, `secret: NAME`), not by value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class ManifestViewError(Exception):
    """The manifest can't be shown; the message says why."""


def _file(kind: str, name: str, path: Path, project: bool = False) -> dict[str, Any]:
    try:
        text = path.read_text()
    except OSError as e:
        text = f"# couldn't read {path}: {e.strerror}"
    home = str(Path.home())
    shown = "~" + str(path)[len(home):] if str(path).startswith(home + "/") else str(path)
    return {"kind": kind, "name": name, "path": shown, "project": project, "text": text}


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


def _routes(bottle: Any, identity: str) -> tuple[list[dict[str, Any]], bool]:
    """The bottle's egress routes, live from the orchestrator when it answers
    (True), else as its files declare them (False)."""
    # pylint: disable-next=import-outside-toplevel
    from ...gateway.egress.schema import load_config
    declared = {route.Host for route in bottle.egress.routes}
    policy = _live_policy(identity)
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


def agent_manifest(identity: str) -> dict[str, Any]:
    """The manifest of the bottle `identity` runs. Raises ManifestViewError."""
    # pylint: disable=import-outside-toplevel
    from ...bottle_state import read_metadata
    from ...manifest.index import ManifestIndex
    from ...manifest.loader import load_bottle_chain, scan_agent_names
    from ...manifest.util import ManifestError
    meta = read_metadata(identity)
    if meta is None:
        raise ManifestViewError("this bottle's launch record is missing")
    try:
        index = ManifestIndex.resolve(meta.cwd or str(Path.home()))
        manifest = index.load_for_agent(meta.agent_name, meta.bottle_names or None)
    except ManifestError as e:
        raise ManifestViewError(f"the manifest doesn't load as it is now: {e}") from e
    files: list[dict[str, Any]] = []
    agents = {**scan_agent_names(index.home_md / "agents")} if index.home_md else {}
    if index.cwd_md is not None:
        agents.update(scan_agent_names(index.cwd_md / "agents"))
    if meta.agent_name in agents:
        files.append(_file("agent", meta.agent_name, agents[meta.agent_name]))
    seen: set[Path] = set()
    names = meta.bottle_names or ((manifest.agent.bottle,) if manifest.agent.bottle else ())
    for name in names:
        try:
            _, sources = load_bottle_chain(name, index.bottle_dirs())
        except ManifestError:
            continue
        for source in sources:
            if source.path not in seen:
                seen.add(source.path)
                files.append(_file("bottle", source.name, source.path, source.project))
    bottle = manifest.bottle
    routes, live = _routes(bottle, identity)
    return {
        "agent": meta.agent_name,
        "bottles": list(names),
        "harness": manifest.agent.agent_provider.harness,
        "files": files,
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


__all__ = ["ManifestViewError", "agent_manifest"]
