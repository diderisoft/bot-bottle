"""Internal bottle `extends:` resolution for manifests."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .manifest import ManifestBottle, ManifestGitEntry
    from .manifest_egress import ManifestEgressConfig


def resolve_bottles(raws: dict[str, dict[str, object]]) -> dict[str, ManifestBottle]:
    """Apply `extends:` chains and return resolved ManifestBottle objects."""
    cache: dict[str, ManifestBottle] = {}
    for name in raws:
        if name not in cache:
            _resolve_one_bottle(name, raws, cache, ())
    return cache


def _resolve_one_bottle(
    name: str,
    raws: dict[str, dict[str, object]],
    cache: dict[str, ManifestBottle],
    seen: tuple[str, ...],
) -> ManifestBottle:
    from .manifest import ManifestBottle, ManifestError

    if name in cache:
        return cache[name]
    if name in seen:
        chain = " -> ".join(seen + (name,))
        raise ManifestError(f"bottle '{name}' is in an extends cycle: {chain}")
    raw = raws[name]
    parent_name_raw = raw.get("extends")
    # Strip `extends:` before passing to ManifestBottle.from_dict so it
    # is not accidentally treated as a real ManifestBottle field by future
    # schema additions. It is only meaningful here.
    child_raw = {k: v for k, v in raw.items() if k != "extends"}

    if parent_name_raw is None:
        bottle = ManifestBottle.from_dict(name, child_raw)
        cache[name] = bottle
        return bottle

    if not isinstance(parent_name_raw, str):
        raise ManifestError(
            f"bottle '{name}' extends must be a string "
            f"(was {type(parent_name_raw).__name__})"
        )
    parent_name: str = parent_name_raw
    if parent_name == name:
        raise ManifestError(
            f"bottle '{name}' extends itself; remove the "
            f"self-reference"
        )
    if parent_name not in raws:
        avail = ", ".join(sorted(raws.keys())) or "(none)"
        raise ManifestError(
            f"bottle '{name}' extends '{parent_name}' which is not "
            f"defined. Available bottles: {avail}"
        )
    parent = _resolve_one_bottle(parent_name, raws, cache, seen + (name,))
    bottle = _merge_bottles(parent, child_raw, name)
    cache[name] = bottle
    return bottle


def _merge_bottles(
    parent: ManifestBottle,
    child_raw: dict[str, object],
    name: str,
) -> ManifestBottle:
    """Apply PRD 0025 merge rules."""
    from .manifest import ManifestBottle, ManifestGitUser
    from .manifest_egress import validate_egress_routes

    # git-gate.repos: union merge at the raw level so child entries can
    # omit fields present on the same-named parent entry (issue #237).
    # After this, child.git already contains the fully merged repo set.
    if _child_declares_git_gate_repos(child_raw):
        child_raw = _merge_git_repos_raw(parent.git, child_raw)

    # Parse the child's declared fields into a ManifestBottle (with the
    # usual defaults for anything missing). Validation runs the same
    # way it would for a leaf bottle: typos / wrong types die here.
    child = ManifestBottle.from_dict(name, child_raw)

    # env: dict merge, child wins on collision.
    merged_env = {**parent.env, **child.env}

    # git-gate.user: per-field overlay. Each non-empty field on child
    # wins; empties fall through to parent. The default ManifestGitUser()
    # is two empty strings, so a child that omits git-gate.user
    # inherits the parent's user verbatim.
    merged_git_user = ManifestGitUser(
        name=child.git_user.name or parent.git_user.name,
        email=child.git_user.email or parent.git_user.email,
    )

    # git-gate.repos: raw union merge already applied above; child.git
    # has the result. An explicit empty repos dict ({}) still clears
    # parent (the raw merge returns early for that case, so child.git
    # stays empty).
    if _child_declares_git_gate_repos(child_raw):
        merged_git = child.git
    else:
        merged_git = parent.git

    # egress.routes: missing means inherit; otherwise parent and child
    # route lists concatenate. Other egress scalar fields remain
    # presence-driven overlays.
    merged_egress = (
        _merge_egress(parent.egress, child.egress, child_raw)
        if "egress" in child_raw
        else parent.egress
    )

    # Presence-driven full-replace for the remaining scalar fields.
    merged_agent_provider = (
        child.agent_provider
        if "agent_provider" in child_raw
        else parent.agent_provider
    )
    merged_supervise = (
        child.supervise if "supervise" in child_raw else parent.supervise
    )
    validate_egress_routes(name, merged_egress.routes)

    return ManifestBottle(
        env=merged_env,
        agent_provider=merged_agent_provider,
        git=merged_git,
        git_user=merged_git_user,
        egress=merged_egress,
        supervise=merged_supervise,
    )


def _entry_to_raw(entry: "ManifestGitEntry") -> dict[str, object]:
    """Convert a ManifestGitEntry back to its YAML-equivalent raw dict."""
    raw: dict[str, object] = {"url": entry.Upstream}
    if entry.KnownHostKey:
        raw["host_key"] = entry.KnownHostKey
    key: dict[str, object] = {"provider": entry.Key.provider}
    if entry.Key.provider == "static":
        key["path"] = entry.Key.path
    else:
        key["forge_token_env"] = entry.Key.forge_token_env
        if entry.Key.api_url:
            key["api_url"] = entry.Key.api_url
    raw["key"] = key
    return raw


def _merge_git_repos_raw(
    parent: "tuple[ManifestGitEntry, ...]",
    child_raw: dict[str, object],
) -> dict[str, object]:
    """Union-merge parent and child git-gate repos at the raw dict level.

    For repos present in both, parent fields provide defaults and child
    fields win. Repos present in only one side pass through unchanged.
    An empty child repos dict is returned as-is to preserve the
    "clear parent repos" semantics."""
    from .manifest_util import as_json_object

    git_raw = as_json_object(child_raw.get("git-gate", {}), "child git-gate")
    child_repos = as_json_object(git_raw.get("repos", {}), "child git-gate.repos")
    if not child_repos:
        return child_raw
    parent_repos = {e.Name: _entry_to_raw(e) for e in parent}
    merged_repos: dict[str, object] = {
        n: {**parent_repos.get(n, {}), **child_repos.get(n, {})}
        for n in set(child_repos) | set(parent_repos)
    }
    return {**child_raw, "git-gate": {**git_raw, "repos": merged_repos}}


def _child_declares_git_gate_repos(child_raw: dict[str, object]) -> bool:
    from .manifest_util import as_json_object

    git_raw = child_raw.get("git-gate")
    if git_raw is None:
        return False
    git_obj = as_json_object(git_raw, "child git-gate")
    return "repos" in git_obj


def _merge_egress(
    parent: ManifestEgressConfig,
    child: ManifestEgressConfig,
    child_raw: dict[str, object],
) -> ManifestEgressConfig:
    from .manifest_egress import ManifestEgressConfig
    from .manifest_util import as_json_object

    child_egress_raw = as_json_object(child_raw.get("egress"), "child egress")
    routes = parent.routes + child.routes
    log = child.Log if "log" in child_egress_raw else parent.Log
    return ManifestEgressConfig(routes=routes, Log=log)
