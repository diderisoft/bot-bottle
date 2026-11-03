"""Bottle `extends:` resolution."""

from __future__ import annotations

import dataclasses

from .bottle import ManifestAudit, ManifestBottle
from .egress import ManifestEgressConfig, overlay_egress, validate_egress_routes
from .forges import (
    ManifestForge, credentials_raw, expand_grants, forges_raw, merge_named, resolve_forge, strip_chain_keys,
)
from .git import ManifestGitUser, parse_git_gate_config
from .legacy_git_gate import OldGitGateFormatError, forges_by_host
from .util import ManifestError, as_json_object


def _overlay_audit(base: ManifestBottle, override: ManifestBottle) -> ManifestAudit:
    """Each `audit:` field the later bottle set, else the earlier's."""
    if "audit" not in override.declared_fields:
        return base.audit
    return ManifestAudit(delivery=override.audit.delivery or base.audit.delivery,
                         firehose=override.audit.firehose or base.audit.firehose)


def _overlay_declared_bool(
    base: ManifestBottle,
    override: ManifestBottle,
    field: str,
) -> bool:
    """Overlay a defaulted boolean only when the override declared it."""
    value = getattr(override if field in override.declared_fields else base, field)
    assert isinstance(value, bool)
    return value


def merge_bottles_runtime(bottles: "list[ManifestBottle]") -> "ManifestBottle":
    """Merge pre-resolved bottles in order, index 0 the base:

    - env: dict merge, later wins
    - git_user: per field, later wins when non-empty
    - repos: union by name, later wins
    - egress routes: per host, later wins; a later block drops the host
    - supervise, nested_containers, preserve_container: later replaces, but
      only when it declared the field (so an explicit `false` overrides)
    """
    if not bottles:
        raise ValueError("merge_bottles_runtime requires at least one bottle")
    result = bottles[0]
    for override in bottles[1:]:
        result = _merge_two_bottles_runtime(result, override)
    return result


def _merge_two_bottles_runtime(base: "ManifestBottle", override: "ManifestBottle") -> "ManifestBottle":
    merged_env = {**base.env, **override.env}

    merged_git_user = ManifestGitUser(
        name=override.git_user.name or base.git_user.name,
        email=override.git_user.email or base.git_user.email,
    )

    # Repos: union by Name; the override wins per name.
    base_repos_by_name = {entry.Name: entry for entry in base.git}
    override_repos_by_name = {entry.Name: entry for entry in override.git}
    merged_repos_names = list(base_repos_by_name) + [
        n for n in override_repos_by_name if n not in base_repos_by_name
    ]
    merged_git = tuple(
        override_repos_by_name[n] if n in override_repos_by_name else base_repos_by_name[n]
        for n in merged_repos_names
    )

    merged_egress = overlay_egress(base.egress, override.egress, override.egress.Log)

    return ManifestBottle(
        env=merged_env,
        git=merged_git,
        git_user=merged_git_user,
        git_trailers=override.git_trailers or base.git_trailers,
        egress=merged_egress,
        supervise=_overlay_declared_bool(base, override, "supervise"),
        nested_containers=_overlay_declared_bool(
            base, override, "nested_containers"
        ),
        preserve_container=_overlay_declared_bool(base, override, "preserve_container"),
        audit=_overlay_audit(base, override),
        forges=_union_forges(base, override),
        declared_fields=base.declared_fields | override.declared_fields,
    )


def _union_forges(base: ManifestBottle, override: ManifestBottle) -> tuple[ManifestForge, ...]:
    by_name = {f.name: f for f in base.forges}
    by_name.update((f.name, f) for f in override.forges)
    return tuple(by_name.values())


def resolve_bottles(raws: dict[str, dict[str, object]]) -> dict[str, ManifestBottle]:
    """Apply `extends:` chains and return the resolved bottles."""
    cache: dict[str, ManifestBottle] = {}
    # Each bottle's effective repos as raw dicts, so a child can merge per
    # field against its parent's.
    repos_cache: dict[str, dict[str, object]] = {}
    # Each bottle's effective credentials and forges, which its repo grants
    # (and its children's) resolve against.
    chain_cache: dict[str, tuple[dict[str, object], dict[str, object]]] = {}
    try:
        for name in raws:
            if name not in cache:
                _resolve_one_bottle(name, raws, cache, repos_cache, (), chain_cache=chain_cache)
    except OldGitGateFormatError as e:
        # The fix depends on which bottles declare the forge; these are the
        # ones loaded (all of them, from a JSON manifest).
        raise e.explained(extends=raws[e.bottle].get("extends"),
                          forge_bottles=forges_by_host(raws.items())) from None
    return cache


def _with_chain(name: str, raw: dict[str, object], parent_names: list[str],
                chain_cache: dict[str, tuple[dict[str, object], dict[str, object]]],
                ) -> dict[str, object]:
    """`raw` with its repo grants expanded against the credentials and forges
    of its chain (parents merged, then its own on top), and those two keys
    removed; records the chain's for its children."""
    creds: dict[str, object] = {}
    forges: dict[str, object] = {}
    for pname in parent_names:
        parent_creds, parent_forges = chain_cache[pname]
        creds = merge_named(creds, parent_creds, "credential")
        forges = merge_named(forges, parent_forges, "forge")
    creds = {**creds, **credentials_raw(name, raw)}
    forges = {**forges, **forges_raw(name, raw)}
    chain_cache[name] = (creds, forges)
    return strip_chain_keys(expand_grants(name, raw, forges, creds))


def _chain_forges(name: str, chain_cache: dict[str, tuple[dict[str, object], dict[str, object]]],
                  ) -> tuple[ManifestForge, ...]:
    creds, forges = chain_cache[name]
    return tuple(resolve_forge(f, raw, creds, name) for f, raw in forges.items())


def _resolve_one_bottle(
    name: str,
    raws: dict[str, dict[str, object]],
    cache: dict[str, ManifestBottle],
    repos_cache: dict[str, dict[str, object]],
    seen: tuple[str, ...],
    *,
    chain_cache: dict[str, tuple[dict[str, object], dict[str, object]]],
) -> ManifestBottle:
    if name in cache:
        return cache[name]
    if name in seen:
        chain = " -> ".join(seen + (name,))
        raise ManifestError(f"bottle '{name}' is in an extends cycle: {chain}")
    raw = raws[name]
    parent_name_raw = raw.get("extends")
    # `extends:` matters only here.
    child_raw = {k: v for k, v in raw.items() if k != "extends"}

    if parent_name_raw is None:
        child_raw = _with_chain(name, child_raw, [], chain_cache)
        bottle = dataclasses.replace(ManifestBottle.from_dict(name, child_raw),
                                     forges=_chain_forges(name, chain_cache))
        cache[name] = bottle
        repos_cache[name] = _resolve_repos_raw({}, child_raw)
        return bottle

    # Accept a str or a list.
    raw_list: list[object]
    if isinstance(parent_name_raw, str):
        raw_list = [parent_name_raw]
    elif isinstance(parent_name_raw, list):
        raw_list = parent_name_raw
    else:
        raise ManifestError(
            f"bottle '{name}' extends must be a string or list of strings "
            f"(was {type(parent_name_raw).__name__})"
        )

    # Validate every entry before resolving any.
    parent_names: list[str] = []
    for i, pname in enumerate(raw_list):
        if not isinstance(pname, str):
            raise ManifestError(
                f"bottle '{name}' extends[{i}] must be a string "
                f"(was {type(pname).__name__})"
            )
        parent_names.append(pname)
        if pname == name:
            raise ManifestError(
                f"bottle '{name}' extends itself; remove the self-reference"
            )
        if pname not in raws:
            avail = ", ".join(sorted(raws.keys())) or "(none)"
            raise ManifestError(
                f"bottle '{name}' extends '{pname}' which is not "
                f"defined. Available bottles: {avail}"
            )

    combined_parent, combined_repos_raw = _fold_parents(
        parent_names, raws, cache, repos_cache, seen + (name,), chain_cache=chain_cache
    )
    child_raw = _with_chain(name, child_raw, parent_names, chain_cache)
    merged_repos_raw = _resolve_repos_raw(combined_repos_raw, child_raw)
    bottle = dataclasses.replace(
        _merge_bottles(combined_parent, child_raw, merged_repos_raw, name),
        forges=_chain_forges(name, chain_cache))
    cache[name] = bottle
    repos_cache[name] = merged_repos_raw
    return bottle


def _fold_parents(
    parent_names: list[str],
    raws: dict[str, dict[str, object]],
    cache: dict[str, ManifestBottle],
    repos_cache: dict[str, dict[str, object]],
    seen: tuple[str, ...],
    *,
    chain_cache: dict[str, tuple[dict[str, object], dict[str, object]]],
) -> tuple[ManifestBottle, dict[str, object]]:
    """Resolve each parent and fold left to right, later winning. `seen`
    carries this bottle's name for cycle detection across every parent edge."""
    first = parent_names[0]
    effective = _resolve_one_bottle(first, raws, cache, repos_cache, seen,
                                    chain_cache=chain_cache)
    effective_repos_raw = repos_cache[first]
    for pname in parent_names[1:]:
        later = _resolve_one_bottle(pname, raws, cache, repos_cache, seen,
                                    chain_cache=chain_cache)
        later_repos_raw = repos_cache[pname]
        effective, effective_repos_raw = _fold_two_bottles(
            effective, effective_repos_raw, later, later_repos_raw
        )
    return effective, effective_repos_raw


def _fold_two_bottles(
    earlier: ManifestBottle,
    earlier_repos_raw: dict[str, object],
    later: ManifestBottle,
    later_repos_raw: dict[str, object],
) -> tuple[ManifestBottle, dict[str, object]]:
    """Combine two resolved parents; later wins."""
    merged_env = {**earlier.env, **later.env}

    merged_git_user = ManifestGitUser(
        name=later.git_user.name or earlier.git_user.name,
        email=later.git_user.email or earlier.git_user.email,
    )

    # Union by name, later wins per field. An empty later set means none
    # declared, not a clear.
    names = list(earlier_repos_raw) + [
        n for n in later_repos_raw if n not in earlier_repos_raw
    ]
    merged_repos_raw: dict[str, object] = {
        n: {
            **as_json_object(earlier_repos_raw.get(n, {}), "earlier parent repo"),
            **as_json_object(later_repos_raw.get(n, {}), "later parent repo"),
        }
        for n in names
    }
    if merged_repos_raw:
        merged_git, _ = parse_git_gate_config("_fold", {"repos": merged_repos_raw})
    else:
        merged_git = ()

    # Routes: per host, later wins; scalars are last-wins.
    merged_egress = overlay_egress(earlier.egress, later.egress, later.egress.Log)

    return ManifestBottle(
        env=merged_env,
        git=merged_git,
        git_user=merged_git_user,
        git_trailers=later.git_trailers or earlier.git_trailers,
        egress=merged_egress,
        supervise=_overlay_declared_bool(earlier, later, "supervise"),
        nested_containers=_overlay_declared_bool(
            earlier, later, "nested_containers"
        ),
        preserve_container=_overlay_declared_bool(earlier, later, "preserve_container"),
        audit=_overlay_audit(earlier, later),
        declared_fields=earlier.declared_fields | later.declared_fields,
    ), merged_repos_raw


def _merge_bottles(
    parent: ManifestBottle,
    child_raw: dict[str, object],
    merged_repos_raw: dict[str, object],
    name: str,
) -> ManifestBottle:
    """Apply the `extends:` merge rules."""
    # A child that declares repos parses with the merged set from
    # `_resolve_repos_raw`.
    if _child_declares_git_gate_repos(child_raw):
        git_raw = as_json_object(child_raw.get("git-gate", {}), "child git-gate")
        child_raw = {**child_raw, "git-gate": {**git_raw, "repos": merged_repos_raw}}

    # Parse the child's fields, validated as for a leaf bottle.
    child = ManifestBottle.from_dict(name, child_raw)

    # env: child wins.
    merged_env = {**parent.env, **child.env}

    # git-gate.user: per field, child wins when non-empty.
    merged_git_user = ManifestGitUser(
        name=child.git_user.name or parent.git_user.name,
        email=child.git_user.email or parent.git_user.email,
    )

    # Declared repos are already merged (an explicit empty dict clears);
    # otherwise inherited.
    if _child_declares_git_gate_repos(child_raw):
        merged_git = child.git
    else:
        merged_git = parent.git

    # Routes: omitted inherits, else the child's replace the parent's per host
    # and its blocks drop them. Other egress fields overlay when present.
    merged_egress = (
        _merge_egress(parent.egress, child.egress, child_raw)
        if "egress" in child_raw
        else parent.egress
    )

    # Remaining scalars: replaced when present.
    merged_supervise = _overlay_declared_bool(parent, child, "supervise")
    merged_nested_containers = _overlay_declared_bool(
        parent, child, "nested_containers"
    )
    validate_egress_routes(name, merged_egress.routes)

    return ManifestBottle(
        env=merged_env,
        git=merged_git,
        git_user=merged_git_user,
        git_trailers=child.git_trailers or parent.git_trailers,
        egress=merged_egress,
        supervise=merged_supervise,
        nested_containers=merged_nested_containers,
        preserve_container=_overlay_declared_bool(parent, child, "preserve_container"),
        audit=_overlay_audit(parent, child),
        declared_fields=parent.declared_fields | child.declared_fields,
    )


def _resolve_repos_raw(
    parent_repos: dict[str, object],
    child_raw: dict[str, object],
) -> dict[str, object]:
    """A bottle's effective repos as raw dicts, by name. Omitted inherits the
    parent's; an explicit empty dict clears; otherwise union by name, child
    fields winning."""
    if not _child_declares_git_gate_repos(child_raw):
        return parent_repos
    child_repos = _declared_repos_raw(child_raw)
    if not child_repos:
        return {}
    # Parent order first, then child-only names.
    names = list(parent_repos) + [n for n in child_repos if n not in parent_repos]
    return {
        name: {
            **as_json_object(parent_repos.get(name, {}), "parent git-gate repo"),
            **as_json_object(child_repos.get(name, {}), "child git-gate repo"),
        }
        for name in names
    }


def _declared_repos_raw(child_raw: dict[str, object]) -> dict[str, object]:
    """The child's declared repos as raw dicts, or {}."""
    if not _child_declares_git_gate_repos(child_raw):
        return {}
    git_raw = as_json_object(child_raw.get("git-gate", {}), "child git-gate")
    return as_json_object(git_raw.get("repos", {}), "child git-gate.repos")


def _child_declares_git_gate_repos(child_raw: dict[str, object]) -> bool:
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
    child_egress_raw = as_json_object(child_raw.get("egress"), "child egress")
    log = child.Log if "log" in child_egress_raw else parent.Log
    return overlay_egress(parent, child, log)
