"""Internal bottle `extends:` resolution for manifests."""

from __future__ import annotations

from .bottle import ManifestAudit, ManifestBottle
from .egress import ManifestEgressConfig, validate_egress_routes
from .git import ManifestGitUser, parse_git_gate_config
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
    """Overlay a defaulted boolean only when override declared it."""
    value = getattr(override if field in override.declared_fields else base, field)
    assert isinstance(value, bool)
    return value


def merge_bottles_runtime(bottles: "list[ManifestBottle]") -> "ManifestBottle":
    """Merge an ordered list of pre-resolved ManifestBottle objects.

    Index 0 is the base; each subsequent entry is applied on top using
    the same field-merge rules as the file-based extends machinery:
      env: dict merge, later wins; git_user: per-field overlay, later
      wins on non-empty; git (repos): union by name, later wins; egress
      routes: concatenate; agent_provider, supervise, nested_containers:
      later replaces (presence-aware).

    Defaulted booleans use presence-aware replacement: if the later bottle
    was loaded from a source that explicitly declared the key, its value
    wins (so an explicit `false` can override an earlier `true`). If the
    later bottle never mentioned the key, the earlier value is preserved.
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

    # git repos: union keyed by Name, override wins per-name.
    base_repos_by_name = {entry.Name: entry for entry in base.git}
    override_repos_by_name = {entry.Name: entry for entry in override.git}
    merged_repos_names = list(base_repos_by_name) + [
        n for n in override_repos_by_name if n not in base_repos_by_name
    ]
    merged_git = tuple(
        override_repos_by_name[n] if n in override_repos_by_name else base_repos_by_name[n]
        for n in merged_repos_names
    )

    merged_routes = base.egress.routes + override.egress.routes
    merged_egress = ManifestEgressConfig(routes=merged_routes, Log=override.egress.Log)

    return ManifestBottle(
        env=merged_env,
        agent_provider=override.agent_provider,
        git=merged_git,
        git_user=merged_git_user,
        egress=merged_egress,
        supervise=_overlay_declared_bool(base, override, "supervise"),
        nested_containers=_overlay_declared_bool(
            base, override, "nested_containers"
        ),
        audit=_overlay_audit(base, override),
        declared_fields=base.declared_fields | override.declared_fields,
    )


def resolve_bottles(raws: dict[str, dict[str, object]]) -> dict[str, ManifestBottle]:
    """Apply `extends:` chains and return resolved ManifestBottle objects."""
    cache: dict[str, ManifestBottle] = {}
    # Per-bottle effective git-gate.repos, as raw dicts keyed by repo name.
    # Threaded alongside `cache` so a child can field-merge against its
    # parent's repos without reconstructing them from parsed entries.
    repos_cache: dict[str, dict[str, object]] = {}
    for name in raws:
        if name not in cache:
            _resolve_one_bottle(name, raws, cache, repos_cache, ())
    return cache


def _resolve_one_bottle(
    name: str,
    raws: dict[str, dict[str, object]],
    cache: dict[str, ManifestBottle],
    repos_cache: dict[str, dict[str, object]],
    seen: tuple[str, ...],
) -> ManifestBottle:
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
        repos_cache[name] = _resolve_repos_raw({}, child_raw)
        return bottle

    # Normalize to list, accepting both str and list[str].
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

    # Validate each entry before resolving any of them.
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
        parent_names, raws, cache, repos_cache, seen + (name,)
    )
    merged_repos_raw = _resolve_repos_raw(combined_repos_raw, child_raw)
    bottle = _merge_bottles(combined_parent, child_raw, merged_repos_raw, name)
    cache[name] = bottle
    repos_cache[name] = merged_repos_raw
    return bottle


def _fold_parents(
    parent_names: list[str],
    raws: dict[str, dict[str, object]],
    cache: dict[str, ManifestBottle],
    repos_cache: dict[str, dict[str, object]],
    seen: tuple[str, ...],
) -> tuple[ManifestBottle, dict[str, object]]:
    """Resolve each parent and fold them left-to-right.

    Later parents win over earlier ones on conflict.  The `seen` tuple
    carries the current bottle's name so cycle detection works across
    every parent edge in the multi-parent graph."""
    first = parent_names[0]
    effective = _resolve_one_bottle(first, raws, cache, repos_cache, seen)
    effective_repos_raw = repos_cache[first]
    for pname in parent_names[1:]:
        later = _resolve_one_bottle(pname, raws, cache, repos_cache, seen)
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
    """Combine two resolved parent bottles; later wins over earlier."""
    merged_env = {**earlier.env, **later.env}

    merged_git_user = ManifestGitUser(
        name=later.git_user.name or earlier.git_user.name,
        email=later.git_user.email or earlier.git_user.email,
    )

    # Repos: union by name; for same-name entries, later wins per-field.
    # Unlike _resolve_repos_raw, an empty later_repos_raw means "no repos
    # declared" — it does NOT clear the earlier parent's repos.
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

    # Egress: routes concatenate; scalar fields use last-wins.
    merged_egress = ManifestEgressConfig(
        routes=earlier.egress.routes + later.egress.routes,
        Log=later.egress.Log,
    )

    return ManifestBottle(
        env=merged_env,
        agent_provider=later.agent_provider,
        git=merged_git,
        git_user=merged_git_user,
        egress=merged_egress,
        supervise=_overlay_declared_bool(earlier, later, "supervise"),
        nested_containers=_overlay_declared_bool(
            earlier, later, "nested_containers"
        ),
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
    # git-gate.repos: when the child declares repos, inject the already
    # name-merged repo set (computed by _resolve_repos_raw) so the child
    # parses with the full inherited+overridden list.
    if _child_declares_git_gate_repos(child_raw):
        git_raw = as_json_object(child_raw.get("git-gate", {}), "child git-gate")
        child_raw = {**child_raw, "git-gate": {**git_raw, "repos": merged_repos_raw}}

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

    # git-gate.repos: when declared, child.git already holds the merged
    # set (an explicit empty dict clears parent, leaving child.git empty).
    # When omitted, the parent's entries are inherited verbatim.
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
    merged_supervise = _overlay_declared_bool(parent, child, "supervise")
    merged_nested_containers = _overlay_declared_bool(
        parent, child, "nested_containers"
    )
    validate_egress_routes(name, merged_egress.routes)

    return ManifestBottle(
        env=merged_env,
        agent_provider=merged_agent_provider,
        git=merged_git,
        git_user=merged_git_user,
        egress=merged_egress,
        supervise=merged_supervise,
        nested_containers=merged_nested_containers,
        audit=_overlay_audit(parent, child),
        declared_fields=parent.declared_fields | child.declared_fields,
    )


def _resolve_repos_raw(
    parent_repos: dict[str, object],
    child_raw: dict[str, object],
) -> dict[str, object]:
    """Compute a bottle's effective git-gate.repos as raw dicts.

    Repos are keyed by name. When the child omits git-gate.repos it
    inherits the parent's set verbatim; an explicit empty dict clears it.
    Otherwise parent and child unite by name, with same-name entries
    field-merged (parent fields are defaults, child fields win)."""
    if not _child_declares_git_gate_repos(child_raw):
        return parent_repos
    child_repos = _declared_repos_raw(child_raw)
    if not child_repos:
        return {}
    # Parent entries keep their order; child-only names are appended.
    names = list(parent_repos) + [n for n in child_repos if n not in parent_repos]
    return {
        name: {
            **as_json_object(parent_repos.get(name, {}), "parent git-gate repo"),
            **as_json_object(child_repos.get(name, {}), "child git-gate repo"),
        }
        for name in names
    }


def _declared_repos_raw(child_raw: dict[str, object]) -> dict[str, object]:
    """Return the child's explicitly declared git-gate.repos as raw dicts,
    or an empty dict when none are declared."""
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
    routes = parent.routes + child.routes
    log = child.Log if "log" in child_egress_raw else parent.Log
    return ManifestEgressConfig(routes=routes, Log=log)
