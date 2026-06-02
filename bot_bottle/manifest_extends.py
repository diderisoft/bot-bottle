"""Internal bottle `extends:` resolution for manifests."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .manifest import Bottle, GitEntry


def resolve_bottles(raws: dict[str, dict[str, object]]) -> dict[str, Bottle]:
    """Apply `extends:` chains and return resolved Bottle objects."""
    cache: dict[str, Bottle] = {}
    for name in raws:
        if name not in cache:
            _resolve_one_bottle(name, raws, cache, ())
    return cache


def _resolve_one_bottle(
    name: str,
    raws: dict[str, dict[str, object]],
    cache: dict[str, Bottle],
    seen: tuple[str, ...],
) -> Bottle:
    from .manifest import Bottle, ManifestError

    if name in cache:
        return cache[name]
    if name in seen:
        chain = " -> ".join(seen + (name,))
        raise ManifestError(f"bottle '{name}' is in an extends cycle: {chain}")
    raw = raws[name]
    parent_name_raw = raw.get("extends")
    # Strip `extends:` before passing to Bottle.from_dict so it
    # is not accidentally treated as a real Bottle field by future
    # schema additions. It is only meaningful here.
    child_raw = {k: v for k, v in raw.items() if k != "extends"}

    if parent_name_raw is None:
        bottle = Bottle.from_dict(name, child_raw)
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
    parent: Bottle,
    child_raw: dict[str, object],
    name: str,
) -> Bottle:
    """Apply PRD 0025 merge rules."""
    from .manifest import Bottle, GitUser, _validate_egress_routes

    # Parse the child's declared fields into a Bottle (with the
    # usual defaults for anything missing). Validation runs the same
    # way it would for a leaf bottle: typos / wrong types die here.
    child = Bottle.from_dict(name, child_raw)

    # env: dict merge, child wins on collision.
    merged_env = {**parent.env, **child.env}

    # git.user: per-field overlay. Each non-empty field on child
    # wins; empties fall through to parent. The default GitUser()
    # is two empty strings, so a child that omits git.user
    # inherits the parent's user verbatim.
    merged_git_user = GitUser(
        name=child.git_user.name or parent.git_user.name,
        email=child.git_user.email or parent.git_user.email,
    )

    # git.remotes: missing means inherit; an explicit empty object
    # clears; otherwise parent and child merge by UpstreamHost with
    # child entries replacing duplicate hosts.
    if _child_declares_git_remotes(child_raw):
        merged_git = _merge_git_remotes(parent.git, child.git) if child.git else ()
    else:
        merged_git = parent.git

    # Presence-driven full-replace for the remaining list-valued +
    # scalar fields.
    merged_egress = child.egress if "egress" in child_raw else parent.egress
    merged_agent_provider = (
        child.agent_provider
        if "agent_provider" in child_raw
        else parent.agent_provider
    )
    merged_supervise = (
        child.supervise if "supervise" in child_raw else parent.supervise
    )
    _validate_egress_routes(name, merged_egress.routes)

    return Bottle(
        env=merged_env,
        agent_provider=merged_agent_provider,
        git=merged_git,
        git_user=merged_git_user,
        egress=merged_egress,
        supervise=merged_supervise,
    )


def _child_declares_git_remotes(child_raw: dict[str, object]) -> bool:
    from .manifest import _as_json_object

    git_raw = child_raw.get("git")
    if git_raw is None:
        return False
    git_obj = _as_json_object(git_raw, "child git")
    return "remotes" in git_obj


def _merge_git_remotes(
    parent: tuple[GitEntry, ...],
    child: tuple[GitEntry, ...],
) -> tuple[GitEntry, ...]:
    by_host = {entry.UpstreamHost: entry for entry in parent}
    for entry in child:
        by_host[entry.UpstreamHost] = entry
    return tuple(by_host.values())
