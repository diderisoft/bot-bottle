"""A running agent's manifest as elements (PRD 0107): each env var, egress
route, repo and forge, with the bottle file it comes from (or "this run"),
and copying or moving picked ones into another bottle.

An element is keyed `<kind>:<name>` (`env:LANG`, `egress:api.example.com`,
`repo:app`, `forge:gitea.example.com`). Where several files in the chain
declare one, the nearest (the child) is its source and the rest are listed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .manifest_view import ManifestViewError
from ...manifest.schema import DEFAULT_BOTTLE


def _chain(identity: str) -> list[dict[str, Any]]:
    """The agent's bottle files, nearest first: name, path, whether it's a
    project's, and whether it can be written (built-ins can't)."""
    # pylint: disable-next=import-outside-toplevel
    from .manifest_view import _index, _load
    meta, _ = _load(identity)
    return _chain_files(meta.bottle_names or (DEFAULT_BOTTLE,), _index(meta).bottle_dirs())


def _chain_files(names: "tuple[str, ...] | list[str]",
                 dirs: "list[tuple[Path, bool]]") -> list[dict[str, Any]]:
    """The files of bottles `names` (merged in that order, so the last
    wins), nearest first: the last bottle's chain child before parents, then
    the one before it's, each file once."""
    # pylint: disable=import-outside-toplevel
    from ...manifest.loader import BUILTIN_BOTTLES_DIR, load_bottle_chain
    from ...manifest.util import ManifestError
    out: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for name in reversed(names):
        try:
            _, sources = load_bottle_chain(name, dirs)
        except ManifestError:
            continue
        for source in sources:
            if source.path in seen:
                continue
            seen.add(source.path)
            builtin = BUILTIN_BOTTLES_DIR in source.path.parents
            out.append({"name": f"./{source.path.stem}" if source.project else source.name,
                        "path": source.path, "project": source.project,
                        "writable": not builtin, "builtin": builtin})
    return out


def _route_detail(route: dict[str, Any]) -> str:
    inspect = route.get("inspect")
    if inspect is False:
        return "not inspected"
    if not isinstance(inspect, dict) or not inspect.get("matches"):
        return "any request"
    parts = []
    for match in inspect["matches"]:
        if not isinstance(match, dict):
            continue
        methods = "/".join(match.get("methods") or []) or "any"
        paths = ", ".join(str(p.get("value", "")) for p in match.get("paths") or []
                          if isinstance(p, dict))
        parts.append(f"{methods} {paths}".strip())
    return "; ".join(parts)


def _file_elements(fm: dict[str, Any]) -> list[tuple[str, str, str, Any]]:
    """A bottle file's elements as (key, label, detail, raw value)."""
    # pylint: disable-next=import-outside-toplevel
    from ...manifest.forges import parse_grant
    out: list[tuple[str, str, str, Any]] = []
    env = fm.get("env")
    for name, value in (env.items() if isinstance(env, dict) else ()):
        out.append((f"env:{name}", name, str(value), value))
    egress = fm.get("egress")
    routes = egress.get("routes") if isinstance(egress, dict) else None
    for route in routes if isinstance(routes, list) else ():
        if isinstance(route, dict) and route.get("host"):
            out.append((f"egress:{route['host']}", str(route["host"]), _route_detail(route),
                        route))
    raw_git = fm.get("git-gate")
    git: dict[str, Any] = raw_git if isinstance(raw_git, dict) else {}
    repos = git.get("repos")
    if isinstance(repos, list):
        for grant in repos:
            if isinstance(grant, str):
                out.append((f"repo:{parse_grant(grant, 'a grant')[2]}",
                            parse_grant(grant, "a grant")[2], grant, grant))
    elif isinstance(repos, dict):
        for name, value in repos.items():
            detail = value if isinstance(value, str) else str(
                value.get("url", "")) if isinstance(value, dict) else ""
            out.append((f"repo:{name}", str(name), detail, value))
    forges = git.get("forges")
    for name, forge in (forges.items() if isinstance(forges, dict) else ()):
        detail = str(forge.get("url", "")) if isinstance(forge, dict) else ""
        out.append((f"forge:{name}", str(name), detail, forge))
    return out


def _run_elements(identity: str, policy: str | None, declared: dict[str, Any],
                  ) -> list[tuple[str, str, str, str, str, bool]]:
    """What the running bottle has beyond its files, as (key, kind, name,
    detail, source, fixed): routes approved and repos granted this run, and
    the agent provider's own routes (its login; not for copying)."""
    # pylint: disable=import-outside-toplevel,protected-access
    from ...approvals import manifest_route
    from ...bottle_state import egress_state_dir, git_gate_state_dir
    from ...gateway.egress.schema import load_config
    from ...git_gate.provision import live_grant_refs, load_upstreams_snapshot
    from .manifest_view import _approved_routes
    out: list[tuple[str, str, str, str, str, bool]] = []
    approved = {r.host for r in _approved_routes(identity, policy)}
    for route in _approved_routes(identity, policy):
        keyed = bool(route.auth_scheme or route.token_env)
        out.append((f"egress:{route.host}", "egress", route.host,
                    ("credential · " if keyed else "") + _route_detail(manifest_route(route)),
                    "this run", keyed))
    try:
        launched = load_config((egress_state_dir(identity) / "routes.yaml")
                               .read_text(encoding="utf-8")).routes
    except (OSError, ValueError):
        launched = ()
    for route in launched:
        key = f"egress:{route.host}"
        if key not in declared and route.host not in approved:
            out.append((key, "egress", route.host,
                        ("credential · " if route.token_env else "")
                        + _route_detail(manifest_route(route)), "agent provider", True))
    stage = git_gate_state_dir(identity)
    upstreams = {u.name: u for u in load_upstreams_snapshot(stage)}
    for ref in live_grant_refs(stage):
        upstream = upstreams.get(ref.name)
        if upstream is not None:
            access = "write" if upstream.writable else "read-only"
            out.append((f"repo:{ref.name}", "repo", ref.name,
                        f"{access} · {ref.grant or upstream.upstream_url}", "this run", False))
            out += _grant_forge(identity, ref.grant, declared, out)
    return out


def _grant_forge(identity: str, grant: str, declared: dict[str, Any],
                 listed: list[tuple[str, str, str, str, str, bool]],
                 ) -> list[tuple[str, str, str, str, str, bool]]:
    """The forge a grant made this run is on, when no file in the chain
    declares it: from the host bottle that does (it stays there)."""
    # pylint: disable-next=import-outside-toplevel
    from .repo_grants import find_forge
    found = find_forge(identity, grant.split("/", 1)[0]) if grant else None
    if found is None or not found.bottle:
        return []
    key = f"forge:{found.forge.name}"
    if key in declared or any(row[0] == key for row in listed):
        return []
    return [(key, "forge", found.forge.name, found.forge.url, found.bottle, True)]


def elements(identity: str, policy: str | None) -> list[dict[str, Any]]:
    """Every element the agent runs with, child bottles' first, then this
    run's changes, each with its `source` and any other files declaring it
    (`also`). `builtin`: from a built-in bottle (copied, never moved);
    `fixed`: can't be written into a bottle (the provider's own, or a route
    carrying a credential)."""
    rows = _file_rows(_chain(identity))
    for key, kind, name, detail, source, fixed in _run_elements(identity, policy, rows):
        row = rows.get(key)
        rows[key] = {"key": key, "kind": kind, "name": name, "detail": detail,
                     "source": source, "builtin": False, "fixed": fixed,
                     "also": ([row["source"], *row["also"]] if row else [])}
    return _ordered(rows)


def bottle_elements(names: "tuple[str, ...] | list[str]",
                    dirs: "list[tuple[Path, bool]]") -> list[dict[str, Any]]:
    """The elements a launch in bottles `names` would run with, before it
    runs (the launch page's preview), shaped as `elements`' are."""
    return _ordered(_file_rows(_chain_files(names, dirs)))


def _file_rows(chain: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each element the chain's files declare, keyed, from the nearest file;
    the farther ones that also declare it are listed in `also`."""
    # pylint: disable-next=import-outside-toplevel
    from ...contract.yaml_subset import parse_frontmatter
    rows: dict[str, dict[str, Any]] = {}
    for f in chain:
        fm = parse_frontmatter(f["path"].read_text(encoding="utf-8"))[0]
        for key, label, detail, _ in _file_elements(dict(fm)):
            if key in rows:
                rows[key]["also"].append(f["name"])
                continue
            rows[key] = {"key": key, "kind": key.split(":", 1)[0], "name": label,
                         "detail": detail, "source": f["name"], "builtin": f["builtin"],
                         "fixed": False, "also": []}
    return rows


def _ordered(rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    order = {"egress": 0, "repo": 1, "forge": 2, "env": 3}
    return sorted(rows.values(), key=lambda r: (order.get(r["kind"], 9), r["name"]))


def _set(fm: dict[str, Any], key: str, value: Any, source_fm: dict[str, Any]) -> None:
    """Put the element `key` (its raw `value`) into `fm`, in place. A route for
    a host it has merges; a forge brings its credential from `source_fm`."""
    # pylint: disable=import-outside-toplevel,protected-access
    from ...approvals import _merge_route
    from ...manifest.forges import parse_grant
    kind, name = key.split(":", 1)
    if kind == "env":
        fm["env"] = {**(fm.get("env") or {}), name: value}
    elif kind == "egress":
        egress = dict(fm.get("egress") or {})
        routes = [r for r in egress.get("routes") or [] if isinstance(r, dict)]
        at = next((i for i, r in enumerate(routes) if r.get("host") == name), None)
        if at is None:
            routes.append(value)
        else:
            routes[at] = _merge_route(routes[at], value)
        egress["routes"] = routes
        fm["egress"] = egress
    elif kind == "repo":
        git = dict(fm.get("git-gate") or {})
        repos = git.get("repos")
        if isinstance(repos, list) and isinstance(value, str):
            repos = [g for g in repos if not (isinstance(g, str)
                                              and parse_grant(g, "a grant")[2] == name)]
            repos.append(value)
        else:
            mapping = ({parse_grant(g, "a grant")[2]: g for g in repos if isinstance(g, str)}
                       if isinstance(repos, list) else dict(repos or {}))
            mapping[name] = value
            repos = mapping
        git["repos"] = repos
        fm["git-gate"] = git
    elif kind == "forge":
        git = dict(fm.get("git-gate") or {})
        git["forges"] = {**(git.get("forges") or {}), name: value}
        fm["git-gate"] = git
        label = str(value.get("token", ""))[1:] if isinstance(value, dict) else ""
        cred = (source_fm.get("credentials") or {}).get(label)
        if label and cred is not None:
            fm["credentials"] = {**(fm.get("credentials") or {}), label: cred}


def _drop(fm: dict[str, Any], key: str) -> None:
    """Take the element `key` out of `fm`, in place."""
    # pylint: disable-next=import-outside-toplevel
    from ...manifest.forges import parse_grant
    kind, name = key.split(":", 1)
    if kind == "env" and isinstance(fm.get("env"), dict):
        fm["env"] = {k: v for k, v in fm["env"].items() if k != name}
    elif kind == "egress" and isinstance(fm.get("egress"), dict):
        egress = dict(fm["egress"])
        egress["routes"] = [r for r in egress.get("routes") or []
                            if not (isinstance(r, dict) and r.get("host") == name)]
        fm["egress"] = egress
    elif kind in ("repo", "forge") and isinstance(fm.get("git-gate"), dict):
        git = dict(fm["git-gate"])
        field = "repos" if kind == "repo" else "forges"
        held = git.get(field)
        if isinstance(held, list):
            git[field] = [g for g in held if not (isinstance(g, str)
                                                  and parse_grant(g, "a grant")[2] == name)]
        elif isinstance(held, dict):
            git[field] = {k: v for k, v in held.items() if k != name}
        fm["git-gate"] = git


def _run_value(identity: str, policy: str | None, key: str) -> Any:
    """This run's raw value for the element `key`, as a bottle file holds it."""
    # pylint: disable=import-outside-toplevel,protected-access
    from ...approvals import manifest_route
    from ...bottle_state import git_gate_state_dir
    from ...git_gate.provision import live_grant_refs, load_upstreams_snapshot
    from .manifest_view import GRANTLESS, _approved_routes
    kind, name = key.split(":", 1)
    if kind == "egress":
        route = next((r for r in _approved_routes(identity, policy) if r.host == name), None)
        if route is not None and not (route.auth_scheme or route.token_env):
            return manifest_route(route)
    if kind == "repo":
        stage = git_gate_state_dir(identity)
        ref = next((r for r in live_grant_refs(stage) if r.name == name), None)
        upstream = next((u for u in load_upstreams_snapshot(stage) if u.name == name), None)
        if ref is not None and upstream is not None:
            if not ref.grant:
                raise ManifestViewError(f"{name} was {GRANTLESS}")
            return ref.grant
    raise ManifestViewError(f"{name} can't be written into a bottle")


def _forge_bottle(forge: str) -> str:
    """The host bottle declaring the forge `forge`, or ""."""
    # pylint: disable=import-outside-toplevel
    from ...contract.yaml_subset import parse_frontmatter
    from ...paths import bot_bottle_root
    for path in sorted((bot_bottle_root() / "bottles").glob("*.md")):
        try:
            fm = parse_frontmatter(path.read_text(encoding="utf-8"))[0]
        except Exception:  # pylint: disable=broad-exception-caught  # not this one
            continue
        git = fm.get("git-gate")
        if isinstance(git, dict) and isinstance(git.get("forges"), dict) \
                and forge in git["forges"]:
            return path.stem
    return ""


def _with_forge_for(dest: Any, fm: dict[str, Any], grant: str) -> None:
    """Make sure the bottle `dest` can resolve the forge of `grant`, extending
    the host bottle that declares it if its chain doesn't. Raises
    ManifestViewError if none does."""
    # pylint: disable=import-outside-toplevel
    from ...manifest.forges import parse_grant
    from ...manifest.loader import BUILTIN_BOTTLES_DIR, load_bottle_chain
    from ...manifest.util import ManifestError
    forge = parse_grant(grant, "a grant")[0]
    declared = isinstance(fm.get("git-gate"), dict) and forge in (
        fm["git-gate"].get("forges") or {})
    if not declared and dest.path.is_file():
        try:
            dirs = [(dest.path.parent, dest.project is not None), (BUILTIN_BOTTLES_DIR, False)]
            chain, _ = load_bottle_chain(dest.path.stem, dirs)
            declared = any(f.name == forge for f in chain.forges)
        except ManifestError:
            declared = False
    if declared:
        return
    holder = _forge_bottle(forge)
    if not holder:
        raise ManifestViewError(f"no bottle on this host declares the forge {forge}")
    raw = fm.get("extends")
    parents = [raw] if isinstance(raw, str) else [str(p) for p in raw] \
        if isinstance(raw, list) else []
    if holder not in parents and holder != dest.path.stem:
        fm["extends"] = [*parents, holder]


def transfer(identity: str, keys: list[str], target: str, *, move: bool) -> dict[str, Any]:
    """Copy (or `move`) the elements `keys` into the bottle `target` (a name
    from `bottle_targets`). Moving also takes them out of the file they came
    from; an element from a built-in bottle, or from this run, is copied.
    Each file changed is saved as a new version (a project's is trusted).
    Raises ManifestViewError."""
    # pylint: disable=import-outside-toplevel,protected-access
    from ...approvals import KeepTarget, bottle_text, read_bottle, save_bottle
    from .manifest_view import _live_policy, _target
    if not keys:
        raise ManifestViewError("pick the elements to copy or move")
    dest = _target(identity, target)
    policy = _live_policy(identity)
    rows = {r["key"]: r for r in elements(identity, policy)}
    chain = {f["name"]: f for f in _chain(identity)}
    dest_fm, dest_body = read_bottle(dest.path)
    dest_fm = dict(dest_fm)
    taken: dict[str, list[str]] = {}
    for key in keys:
        row = rows.get(key)
        if row is None:
            raise ManifestViewError(f"there's no {key} to copy")
        if row["source"] == "agent provider":
            raise ManifestViewError(f"{row['name']} is the agent provider's own route; "
                                    f"it can't be written into a bottle")
        if row["source"] == "this run":
            value = _run_value(identity, policy, key)
            if key.startswith("repo:") and isinstance(value, str):
                _with_forge_for(dest, dest_fm, value)
            _set(dest_fm, key, value, {})
            continue
        source = chain[row["source"]]
        if source["path"] == dest.path:
            raise ManifestViewError(f"{row['name']} is already in {target}")
        source_fm = dict(read_bottle(source["path"])[0])
        value = next(v for k, _, _, v in _file_elements(source_fm) if k == key)
        if key.startswith("repo:") and isinstance(value, str):
            _with_forge_for(dest, dest_fm, value)
        _set(dest_fm, key, value, source_fm)
        if move and source["writable"]:
            taken.setdefault(row["source"], []).append(key)
    names = ", ".join(rows[k]["name"] for k in keys)
    save_bottle(dest, bottle_text(dest_fm, dest_body),
                f"{'move' if move else 'copy'} from the console: {names}")
    for source_name, source_keys in taken.items():
        source = chain[source_name]
        fm, body = read_bottle(source["path"])
        fm = dict(fm)
        for key in source_keys:
            _drop(fm, key)
        project = source["path"].parents[2] if source["project"] else None
        save_bottle(KeepTarget(source["path"], project), bottle_text(fm, body),
                    f"move to {target} from the console: "
                    f"{', '.join(rows[k]['name'] for k in source_keys)}")
    return {"into": dest.label() if dest.project is None else f"./{dest.path.stem}",
            "moved_from": sorted(taken), "count": len(keys)}


def delete(identity: str, keys: list[str], *, backend: Any = None) -> dict[str, Any]:
    """Delete the elements `keys`: out of the bottle file each comes from (a
    new version; the running agent keeps it until its next launch), or, for
    this run's, out of the running bottle (`manifest_view.remove_from_run`).
    A built-in bottle's, and the agent provider's, stay. Raises
    ManifestViewError."""
    # pylint: disable=import-outside-toplevel
    from ...approvals import KeepTarget, bottle_text, read_bottle, save_bottle
    from .manifest_view import _live_policy, remove_from_run
    if not keys:
        raise ManifestViewError("pick the elements to delete")
    policy = _live_policy(identity)
    rows = {r["key"]: r for r in elements(identity, policy)}
    chain = {f["name"]: f for f in _chain(identity)}
    from_files: dict[str, list[str]] = {}
    from_run: list[str] = []
    for key in keys:
        row = rows.get(key)
        if row is None:
            raise ManifestViewError(f"there's no {key} to delete")
        if row["fixed"] or row["source"] == "agent provider":
            raise ManifestViewError(f"{row['name']} can't be deleted here")
        if row["source"] == "this run":
            from_run.append(key)
        elif not chain[row["source"]]["writable"]:
            raise ManifestViewError(f"{row['name']} is in the built-in bottle "
                                    f"{row['source']}, which can't be changed")
        else:
            from_files.setdefault(row["source"], []).append(key)
    for source_name, source_keys in from_files.items():
        source = chain[source_name]
        fm, body = read_bottle(source["path"])
        fm = dict(fm)
        for key in source_keys:
            _drop(fm, key)
        project = source["path"].parents[2] if source["project"] else None
        save_bottle(KeepTarget(source["path"], project), bottle_text(fm, body),
                    f"delete from the console: "
                    f"{', '.join(rows[k]['name'] for k in source_keys)}")
    removed = remove_from_run(identity, policy, from_run, backend=backend)["removed"] \
        if from_run else []
    return {"deleted_from": sorted(from_files), "removed": removed, "count": len(keys)}


__all__ = ["delete", "elements", "transfer"]
