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
from ...manifest.schema import DEFAULT_BOTTLE


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
    from ..client import OrchestratorClient, OrchestratorClientError, env_orchestrator_url
    adopted = read_adoption(identity)
    if adopted is None:
        return None
    try:
        records = OrchestratorClient(env_orchestrator_url()).list_bottles()
    except (OrchestratorClientError, OSError):
        return None
    policy = next((r.get("policy") for r in records
                   if r.get("bottle_id") == adopted.registry_id), None)
    return policy if isinstance(policy, str) else None


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


# Why a repo granted with no grant on record can't go into a bottle: its key is
# forge-minted, and a bottle holds one of those only as a grant (ADR 0008).
GRANTLESS = "granted with no grant on record, so no bottle can hold it; ask for it again"


def _with_run_changes(identity: str, policy: str | None, frontmatter: dict[str, object],
                      only: "set[str] | None" = None,
                      ) -> tuple[dict[str, object], list[dict[str, str]], list[dict[str, str]]]:
    """`frontmatter` with this run's changes applied as a bottle file holds
    them: egress approved since launch, and repos granted while it runs (as
    grants, extending the bottle that declares their forge if needed). Returns
    it with the changes that altered it and the ones that can't be kept.
    `only` limits it to those change keys (`egress:<host>`, `repo:<name>`)."""
    # pylint: disable=import-outside-toplevel
    from ...approvals import with_grant, with_routes
    from ...bottle_state import git_gate_state_dir
    from ...git_gate.provision import live_grant_refs, load_upstreams_snapshot
    from .repo_grants import find_forge
    fm = frontmatter
    changes: list[dict[str, str]] = []
    unkept: list[dict[str, str]] = []
    for route in _approved_routes(identity, policy):
        key = f"egress:{route.host}"
        if route.auth_scheme or route.token_env:
            unkept.append({"kind": "egress", "what": route.host, "key": key,
                           "why": "carries a credential, so it's allowed once only"})
            continue
        if only is not None and key not in only:
            continue
        after = with_routes(fm, [route])
        if after != fm:
            fm = after
            changes.append({"kind": "egress", "what": route.host, "key": key})
    stage = git_gate_state_dir(identity)
    upstreams = {u.name: u for u in load_upstreams_snapshot(stage)}
    for ref in live_grant_refs(stage):
        key = f"repo:{ref.name}"
        if only is not None and key not in only:
            continue
        if not ref.grant:
            # A forge-minted key goes in a bottle only as a grant (ADR 0008).
            if ref.name in upstreams:
                unkept.append({"kind": "repo", "what": ref.name, "key": key,
                               "why": GRANTLESS})
            continue
        found = find_forge(identity, ref.grant.split("/", 1)[0])
        after, what = with_grant(fm, ref.grant, extends=found.bottle if found else ""), \
            ref.grant
        if after != fm:
            fm = after
            changes.append({"kind": "repo", "what": what, "key": key})
    return fm, changes, unkept


def bottle_with_changes(identity: str, policy: str | None, target: Any,
                        only: "set[str] | None" = None) -> dict[str, Any]:
    """The bottle file `target` (an `approvals.KeepTarget`) as it would read
    with this run's changes (or `only` those) written into it."""
    # pylint: disable=import-outside-toplevel
    import hashlib
    from ...approvals import bottle_text, read_bottle
    original = target.path.read_text(encoding="utf-8") if target.path.is_file() else ""
    fm, body = read_bottle(target.path)
    fm, changes, unkept = _with_run_changes(identity, policy, fm, only)
    updated = bottle_text(fm, body) if changes else original
    return {"target": target.label(), "path": _shown(target.path),
            "project": target.project is not None, "exists": target.path.is_file(),
            "original": original, "updated": updated, "data": fm, "changes": changes,
            "unkept": unkept, "digest": hashlib.sha256(updated.encode()).hexdigest()}


def running_bottle(identity: str, policy: str | None) -> dict[str, Any] | None:
    """The bottle "keep" writes to (`approvals.keep_target`) with this run's
    changes, or None when the run has nowhere to keep things."""
    # pylint: disable-next=import-outside-toplevel
    from ...approvals import keep_target
    target = keep_target(identity)
    return None if target is None else bottle_with_changes(identity, policy, target)


def _targets(identity: str) -> list[tuple[Any, bool]]:
    """The bottle files the run's changes can be written into, with whether
    each is in the agent's chain: the chain's own first (child before
    parents), then the host's other bottles. Built-in bottles aren't files
    to write."""
    # pylint: disable=import-outside-toplevel
    from ...approvals import KeepTarget
    from ...manifest.loader import BUILTIN_BOTTLES_DIR, load_bottle_chain
    from ...manifest.util import ManifestError
    from ...paths import bot_bottle_root
    meta, _ = _load(identity)
    index_dirs = _index(meta).bottle_dirs()
    out: list[tuple[Any, bool]] = []
    seen: set[Path] = set()
    for name in meta.bottle_names or (DEFAULT_BOTTLE,):
        try:
            _, sources = load_bottle_chain(name, index_dirs)
        except ManifestError:
            continue
        for source in sources:
            if source.path in seen or BUILTIN_BOTTLES_DIR in source.path.parents:
                continue
            seen.add(source.path)
            out.append((KeepTarget(source.path, source.path.parents[2] if source.project
                                   else None), True))
    for path in sorted((bot_bottle_root() / "bottles").glob("*.md")):
        if path not in seen:
            out.append((KeepTarget(path, None), False))
    return out


def bottle_targets(identity: str, policy: str | None) -> list[dict[str, Any]]:
    """Each bottle the run's changes could be written into, and how many of
    them it would take."""
    out = []
    for t, in_chain in _targets(identity):
        keys = [c["key"] for c in bottle_with_changes(identity, policy, t)["changes"]]
        out.append({"name": t.label() if t.project is None else f"./{t.path.stem}",
                    "label": t.label(), "path": _shown(t.path),
                    "project": t.project is not None, "in_chain": in_chain,
                    "changes": len(keys), "keys": keys})
    return out


def _index(meta: Any) -> Any:
    # pylint: disable-next=import-outside-toplevel
    from ...manifest.index import ManifestIndex
    return ManifestIndex.resolve(meta.cwd or str(Path.home()))


def _load(identity: str) -> tuple[Any, Any]:
    """The bottle's launch record and its agent's manifest as the files are
    now. Raises ManifestViewError."""
    # pylint: disable=import-outside-toplevel
    from ...bottle_state import read_metadata
    from ...manifest.util import ManifestError
    meta = read_metadata(identity)
    if meta is None:
        raise ManifestViewError("this bottle's launch record is missing")
    try:
        return meta, _index(meta).load_for_agent(meta.agent_name, meta.bottle_names or None)
    except ManifestError as e:
        raise ManifestViewError(f"the manifest doesn't load as it is now: {e}") from e


def _granted(identity: str, doc: dict[str, object], ref: Any) -> str:
    """A live grant as its grant, with the forge it's on put into `doc` (in
    place) from whichever bottle declares it; "" when it has no grant or the
    forge can't be found, so no bottle can hold it."""
    # pylint: disable-next=import-outside-toplevel
    from ...manifest.document import forge_document
    from .repo_grants import find_forge
    if not ref.grant:
        return ""
    found = find_forge(identity, ref.grant.split("/", 1)[0])
    if found is None:
        return ""
    git = _dict(doc.get("git-gate"))
    forges = _dict(git.get("forges"))
    if found.forge.name not in forges:
        label, credential, forges[found.forge.name] = forge_document(found.forge)
        doc["credentials"] = {**_dict(doc.get("credentials")), label: credential}
        git["forges"] = forges
        doc["git-gate"] = git
    return str(ref.grant)


def _dict(value: object) -> dict[str, object]:
    """A copy of a mapping from a document, or {} for anything else."""
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def current_manifest(identity: str, bottle: Any, policy: str | None,
                     only: "set[str] | None" = None) -> dict[str, Any]:
    """The agent's latest manifest version, and the manifest it runs with now:
    that version with the supervisor's changes since (egress approved, repos
    granted). A route carrying a credential is allowed once only, so it's
    listed, not added. A bottle launched before versions were kept gets
    version 1 from its files as they are now. `only` limits it to those change
    keys (`egress:<host>`, `repo:<name>`)."""
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
        key = f"egress:{route.host}"
        if route.auth_scheme or route.token_env:
            unkept.append({"kind": "egress", "what": route.host, "key": key,
                           "why": "carries a credential, so it's allowed once only"})
            continue
        if only is not None and key not in only:
            continue
        after = with_routes(doc, [route])
        if after != doc:
            doc = after
            changes.append({"kind": "egress", "what": route.host, "key": key})
    stage = git_gate_state_dir(identity)
    upstreams = {u.name: u for u in load_upstreams_snapshot(stage)}
    for ref in live_grant_refs(stage):
        upstream = upstreams.get(ref.name)
        if upstream is None or (only is not None and f"repo:{ref.name}" not in only):
            continue
        repo = _granted(identity, doc, ref)
        if not repo:
            # Not as a grant, so not in a document that may become a bottle.
            unkept.append({"kind": "repo", "what": ref.name, "key": f"repo:{ref.name}",
                           "why": GRANTLESS if not ref.grant else
                           "its forge isn't declared in any bottle now"})
            continue
        git = _dict(doc.get("git-gate"))
        repos = _dict(git.get("repos"))
        if repos.get(ref.name) != repo:
            repos[ref.name] = repo
            git["repos"] = repos
            doc["git-gate"] = git
            access = "write" if upstream.writable else "read-only"
            changes.append({"kind": "repo", "what": f"{ref.name} ({access})",
                            "key": f"repo:{ref.name}"})
    text = run_manifest.document_text(doc)
    return {"version": entry["version"], "saved_at": entry.get("at", ""),
            "message": entry.get("message", ""), "versions": len(run_manifest.versions(identity)),
            "base": run_manifest.document_text(base), "text": text, "data": doc,
            "changes": changes, "unkept": unkept,
            "digest": hashlib.sha256(text.encode()).hexdigest()}


def _checked(identity: str, digest: str) -> tuple[Any, Any, str | None, dict[str, Any]]:
    """The launch record, manifest, live policy and current manifest, if the
    current manifest is still the one the console showed. Raises
    ManifestViewError."""
    meta, manifest = _load(identity)
    policy = _live_policy(identity)
    current = current_manifest(identity, manifest.bottle, policy)
    if current["digest"] != digest:
        raise ManifestViewError("the manifest changed since it was shown; refresh and try again")
    return meta, manifest, policy, current


def _target(identity: str, name: str) -> Any:
    target = next((t for t, _ in _targets(identity)
                   if (t.label() if t.project is None else f"./{t.path.stem}") == name), None)
    if target is None:
        raise ManifestViewError(f"there's no bottle {name} to write into")
    return target


def save_manifest(identity: str, digest: str, *, bottle: str = "",
                  keys: "list[str] | None" = None) -> dict[str, Any]:
    """Save the manifest the agent runs with now, with this run's changes (or
    only `keys` of them), as its next version, if it's still what the console
    showed (`digest`); with `bottle` (a name from `bottle_targets`), also write
    those changes into that bottle file. Raises ManifestViewError."""
    # pylint: disable=import-outside-toplevel
    from ... import run_manifest
    from ...approvals import save_bottle
    _, manifest, policy, _ = _checked(identity, digest)
    only = set(keys) if keys is not None else None
    chosen = current_manifest(identity, manifest.bottle, policy, only)
    if not chosen["changes"]:
        raise ManifestViewError("there's nothing new to save" if only is None
                                else "none of the picked changes can be saved")
    into = None
    if bottle:
        # Checked before anything is written, so a save is all or nothing.
        into = bottle_with_changes(identity, policy, _target(identity, bottle), only)
        if not into["changes"]:
            raise ManifestViewError(f"{into['target']} already has the picked changes, "
                                    f"or can't take them; nothing was saved")
    whats = ", ".join(c["what"] for c in chosen["changes"])
    saved: dict[str, Any] = {"version": run_manifest.record(identity, chosen["data"],
                                                            f"saved: {whats}")}
    if into is not None:
        save_bottle(_target(identity, bottle), into["updated"],
                    f"keep from the console: {', '.join(c['what'] for c in into['changes'])}")
        saved["bottle"] = into["target"]
    return saved


def remove_changes(identity: str, digest: str, keys: list[str], *,
                   backend: Any = None) -> dict[str, Any]:
    """`remove_from_run`, if the manifest is still the one the console showed
    (`digest`). Raises ManifestViewError."""
    _, _, policy, _ = _checked(identity, digest)
    return remove_from_run(identity, policy, keys, backend=backend)


def remove_from_run(identity: str, policy: str | None, keys: list[str], *,
                    backend: Any = None) -> dict[str, Any]:
    """Take this run's changes `keys` away from the running bottle: an egress
    route goes back to what the bottle launched with (or goes), a repo granted
    while it runs is revoked (through `backend`, the one the bottle runs on).
    Raises ManifestViewError."""
    # pylint: disable=import-outside-toplevel
    from ...backend.adoption import read as read_adoption
    from ...bottle_state import egress_state_dir, git_gate_state_dir
    from ...egress.service import egress_render_routes
    from ...gateway.egress.schema import load_config
    from ...git_gate.live_grant import GrantError, revoke_live
    from ...secret_source import FileSecrets
    from ...paths import host_secrets_path
    from ..client import OrchestratorClient, env_orchestrator_url
    hosts = {k.split(":", 1)[1] for k in keys if k.startswith("egress:")}
    repos = [k.split(":", 1)[1] for k in keys if k.startswith("repo:")]
    if not hosts and not repos:
        raise ManifestViewError("pick the changes to remove")
    adopted = read_adoption(identity)
    if adopted is None:
        raise ManifestViewError("can't reach the running bottle")
    removed: list[str] = []
    if hosts:
        if policy is None:
            raise ManifestViewError("the orchestrator didn't answer for the bottle's routes")
        live = load_config(policy)
        if live.deny_reason:
            raise ManifestViewError("this bottle's routes can't be changed from here")
        launched = {r.host: r for r in load_config(
            (egress_state_dir(identity) / "routes.yaml").read_text(encoding="utf-8")).routes}
        routes = [launched[r.host] if r.host in hosts and r.host in launched else r
                  for r in live.routes if r.host not in hosts or r.host in launched]
        text = egress_render_routes(tuple(routes), log=live.log,  # type: ignore[arg-type]
                                    audit_delivery=live.audit_delivery, firehose=live.firehose)
        OrchestratorClient(env_orchestrator_url()).set_policy(
            adopted.registry_id, text)
        removed += sorted(hosts)
    if repos:
        if backend is None:
            raise ManifestViewError("this host controller can't remove repos")
        try:
            guest = backend.attach_adopted(identity, adopted)
            transport = backend.infra_service().gateway().provisioning_transport()
            for name in repos:
                revoke_live(name, stage_dir=git_gate_state_dir(identity),
                            secrets=FileSecrets(host_secrets_path()), transport=transport,
                            registry_id=adopted.registry_id, bottle=guest,
                            launched=tuple(r.name for r in adopted.deploy_keys))
                removed.append(name)
        except GrantError as e:
            raise ManifestViewError(f"{e} (removed so far: {', '.join(removed) or 'none'})") \
                from e
    return {"removed": removed}


_HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                   r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


def allow_in_run(identity: str, host: str, *, keep: bool = False) -> dict[str, Any]:
    """Let the running bottle reach `host`, which it was blocked from: the
    operator's own allow from the console, with no request from the agent. The
    route is the default one (read-only unless it injects a credential); with
    `keep`, it goes into the bottle file the agent's approvals are kept in too.
    Raises ManifestViewError."""
    # pylint: disable=import-outside-toplevel
    from ...approvals import _add_routes, keep_target
    from ...backend.adoption import read as read_adoption
    from ...egress.service import egress_render_routes
    from ...gateway.egress.schema import load_config
    from ...gateway.egress.types import Route
    from ..client import OrchestratorClient, env_orchestrator_url
    host = host.strip().lower()
    if not _HOST.match(host):
        raise ManifestViewError(f"{host!r} isn't a host name")
    adopted = read_adoption(identity)
    policy = _live_policy(identity)
    if adopted is None or policy is None:
        raise ManifestViewError("can't reach the running bottle")
    live = load_config(policy)
    if live.deny_reason:
        raise ManifestViewError("this bottle's routes can't be changed from here")
    if any(r.host.lower() == host for r in live.routes):
        raise ManifestViewError(f"{host} already has a route; change it in the Bottle tab")
    text = egress_render_routes((*live.routes, Route(host=host)),  # type: ignore[arg-type]
                                log=live.log, audit_delivery=live.audit_delivery,
                                firehose=live.firehose)
    OrchestratorClient(env_orchestrator_url()).set_policy(adopted.registry_id, text)
    reply: dict[str, Any] = {"allowed": host}
    if keep:
        target = keep_target(identity)
        if target is None:
            raise ManifestViewError(f"allowed {host} for this run, but there's no bottle "
                                    "to keep it in")
        _add_routes(target.path, [Route(host=host)])
        reply["kept"] = target.label()
    return reply


_BOTTLE_NAME =re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def save_as_bottle(identity: str, digest: str, name: str, *,
                   extend: bool = False, keys: "list[str] | None" = None) -> dict[str, Any]:
    """Write the manifest the agent runs with now as a new host bottle `name`,
    if it's still what the console showed, and record it as the agent's next
    version. Flattened, it stands alone (the whole chain, no `extends`); with
    `extend`, it extends the bottles it launched in and holds only this run's
    changes. Raises ManifestViewError."""
    # pylint: disable=import-outside-toplevel
    from ... import run_manifest
    from ...approvals import KeepTarget, bottle_text, save_bottle
    from ...paths import bot_bottle_root
    if not _BOTTLE_NAME.match(name) or name.endswith(".md"):
        raise ManifestViewError("a bottle name is letters, digits, '.', '_' and '-'")
    path = bot_bottle_root() / "bottles" / f"{name}.md"
    if path.exists():
        raise ManifestViewError(f"there's already a bottle named {name}")
    meta, manifest, policy, _ = _checked(identity, digest)
    only = set(keys) if keys is not None else None
    current = current_manifest(identity, manifest.bottle, policy, only)
    if extend:
        parents = list(meta.bottle_names or (DEFAULT_BOTTLE,))
        if any(p.startswith("./") for p in parents):
            raise ManifestViewError("a host bottle can't extend a project's bottle; "
                                    "save it flattened instead")
        fm, _, _ = _with_run_changes(identity, policy, {"extends": parents}, only)
        body = (f"Saved from {meta.agent_name}'s run ({identity}) in the console: "
                f"{', '.join(parents)} with what the run was approved or granted.")
    else:
        fm = current["data"]
        body = (f"Saved from {meta.agent_name}'s run ({identity}) in the console: everything "
                f"it ran with, its bottles flattened into one.")
    save_bottle(KeepTarget(path, None), bottle_text(fm, body),
                f"save {identity}'s manifest as {name}")
    return {"version": run_manifest.record(identity, current["data"], f"saved as bottle {name}"),
            "new_bottle": name}


def _elements(identity: str, policy: str | None) -> list[dict[str, Any]]:
    # pylint: disable-next=import-outside-toplevel,cyclic-import
    from .manifest_elements import elements
    return elements(identity, policy)


def bottle_versions(identity: str, meta: Any, names: "tuple[str, ...]") -> list[dict[str, Any]]:
    """Each bottle of the agent's chain, in chain order: whether it still
    resolves to what it launched with (`latest`; `known` is false for a run
    launched before that was recorded) and, for a host bottle, the host's
    version then (`version`, `version_at`). A project's bottle is versioned
    by its repo and a built-in one by bot-bottle, so neither has one."""
    # pylint: disable=import-outside-toplevel
    from ... import run_manifest
    from ...manifest.fingerprint import chain_hashes
    from ...manifest.loader import BUILTIN_BOTTLES_DIR, PROJECT_PREFIX, find_bottle
    from ...manifest.versions import committed_at
    launch = run_manifest.launch_record(identity)
    raw = launch.get("bottles")
    launched: dict[str, Any] = raw if isinstance(raw, dict) else {}
    host = str(launch.get("host") or "")
    index = _index(meta)
    now = chain_hashes(index, names)
    at = committed_at(host) if host else 0
    out = []
    for name in names:
        found = find_bottle(name, index.bottle_dirs()) if index.home_md is not None else None
        builtin = found is not None and BUILTIN_BOTTLES_DIR in found[0].parents
        project = name.startswith(PROJECT_PREFIX)
        versioned = bool(host) and not (project or builtin)
        out.append({"name": name, "project": project, "builtin": builtin,
                    "known": name in launched,
                    "latest": name in launched and launched[name] == now.get(name),
                    "version": host if versioned else "", "version_at": at if versioned else 0})
    return out


def agent_manifest(identity: str) -> dict[str, Any]:
    """The manifest of the bottle `identity` runs: what it runs with now
    against its last saved version (`current`), and the bottles the run's
    changes could also be written into (`bottle_targets`). Raises
    ManifestViewError."""
    meta, manifest = _load(identity)
    bottle = manifest.bottle
    policy = _live_policy(identity)
    names = meta.bottle_names or (DEFAULT_BOTTLE,)
    return {
        "current": current_manifest(identity, bottle, policy),
        "bottle_targets": bottle_targets(identity, policy),
        "elements": _elements(identity, policy),
        "agent": meta.agent_name,
        "bottles": list(names),
        "bottle_versions": bottle_versions(identity, meta, tuple(names)),
        "harness": manifest.agent.agent_provider.harness,
    }


__all__ = ["ManifestViewError", "agent_manifest", "allow_in_run", "bottle_targets",
           "bottle_versions",
           "bottle_with_changes",
           "current_manifest", "remove_changes", "remove_from_run", "running_bottle",
           "save_as_bottle", "save_manifest"]
