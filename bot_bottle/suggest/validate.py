"""Convert a Proposal to a bottle document and validate it with the manifest
loader (PRD 0117).

Elements that the loader rejects are dropped and returned as notes rather than
failing the whole proposal.  `inspect: false` is never proposed (PRD 0117
security constraint).

The manufactured bottle is temporary (in-memory frontmatter only); it is never
written to disk here.
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..contract.yaml_subset import serialize_yaml_subset
from ..manifest.extends import merge_bottles_runtime
from ..manifest.loader import BUILTIN_BOTTLES_DIR, find_bottle, load_bottle_chain
from ..manifest.util import ManifestError
from ..secret_source import SecretSource
from ..secret_ref import stored
from .covered import drop_covered
from .edits import SECRET_NAME, overlay, stack_name, validate_edits
from .edits import unconnected as unconnected_forges
from .forges import Described, connected_hosts, described_forges, grant_forge
from .runnable import make_runnable
from .proposal import (
    OPTION_NEW,
    OPTION_NEW_EXTENDING,
    Proposal,
    ProposalAdditions,
    ProposalCredential,
    ProposalEgress,
)


def _egress_routes(additions: ProposalAdditions) -> list[dict[str, Any]]:
    """Additions' egress entries as bottle frontmatter routes (never
    passthrough)."""
    routes = []
    cred_by_host = {c.route: c for c in additions.credentials}
    for e in additions.egress:
        route: dict[str, Any] = {"host": e.host}
        cred = cred_by_host.get(e.host)
        if e.methods:
            route["methods"] = list(e.methods)
        elif cred:
            route["methods"] = ["*"]  # an API with a credential is written to
        if cred:
            route["auth"] = {"type": "bearer", "value": stored(cred.name)}
        routes.append(route)
    return routes


def _forge_descriptions(grants: list[str], unconnected: Described) -> dict[str, Any]:
    """How a bottle describes the forges `grants` are on: a connected one as
    this host connected it, else as `unconnected` describes it."""
    # pylint: disable=import-outside-toplevel
    from ..forge_connections import get
    from ..manifest.forges import described_connection, parse_grant, split_grant_alias
    out: dict[str, Any] = {}
    for grant in grants:
        try:
            host = parse_grant(split_grant_alias(grant)[1], "a grant")[0]
            conn = get(host)
        except ManifestError:
            continue
        if host in out:
            continue
        if conn is not None:
            out[host] = described_connection(conn)
        elif host in unconnected:
            out[host] = dict(unconnected[host])
    return out


def proposal_to_frontmatter(proposal: Proposal) -> dict[str, Any]:
    """A bottle frontmatter dict for the proposal's additions.

    For reuse/stack options this is an additions-only dict, stacked after
    `proposal.bottles` exactly as a launch stacks bottles (`stack_names`).
    Repo grants come with their forges' descriptions, from this host's
    connections, so the bottle says where they go on any machine (ADR 0019).
    For new-extending / new it includes the `extends` from `new_bottle`.
    """
    fm: dict[str, Any] = {}

    base_bottles = list(proposal.bottles)
    if proposal.option in (OPTION_NEW_EXTENDING, OPTION_NEW) and proposal.new_bottle:
        extends = proposal.new_bottle.extends or (base_bottles[0] if base_bottles else "basic")
        fm["extends"] = extends

    add = proposal.additions
    env: dict[str, str] = {}
    for v in add.env:
        env[v.name] = stored(v.name)  # the host's saved value, by name
    if env:
        fm["env"] = env

    repos = [r.grant for r in add.repos]
    if repos:
        git: dict[str, Any] = {"repos": repos}
        forges = _forge_descriptions(repos, proposal.forges)
        if forges:
            git["forges"] = forges
        fm["git-gate"] = git

    routes = _egress_routes(add)
    if routes:
        fm["egress"] = {"routes": routes}

    return fm


def validate_proposal(
    proposal: Proposal,
    dirs: list[tuple[Path, bool]] | None = None,
    secrets: SecretSource | None = None,
    described: Described | None = None,
) -> tuple[Proposal, list[str]]:
    """Validate the proposal by trying to load it with the manifest loader.

    First it's made runnable here (`runnable`): bottles whose secrets aren't
    in `secrets` (the host's, by default) are dropped, and `blocked` says when it still can't
    run. Then invalid additions are dropped individually (first egress
    routes, then repo grants, then env vars) and returned as notes.  The
    validated Proposal and accumulated notes are returned.

    `dirs` is the bottle search path as `(directory, is_project)` pairs (same
    as `ManifestIndex.bottle_dirs()`).  When None, only built-in bottles are
    available (sufficient for rules-path tests).

    A repo grant on a forge this host hasn't connected is kept when the forge
    is known: a bottle in `dirs` describes it, `described` does (the
    project's origin; `suggest.forges`), or an earlier validation did
    (`proposal.forges`). Its description goes in `forges`, for the review to
    offer connecting it; a grant on a forge nothing describes is dropped.
    """
    if dirs is None:
        dirs = [(BUILTIN_BOTTLES_DIR, False)]
    known = {**proposal.forges, **described_forges(dirs), **(described or {})}
    for edit in proposal.edits:
        known = {**edit.forges, **known}
    edits, edit_notes = validate_edits(proposal.edits, dirs, known)
    before = list(proposal.notes)
    proposal = replace(proposal, notes=before + edit_notes,
                       bottles=[stack_name(b) for b in proposal.bottles])
    with overlay(edits, dirs, known, loadable=True) as view:
        cleaned = _validate(proposal, view, secrets, known)
    cleaned.edits = edits
    for edit in edits:
        for host in unconnected_forges(edit):
            cleaned.forges.setdefault(host, dict(edit.forges.get(host) or known.get(host, {})))
    if edit_notes:
        cleaned.summary = ""  # it described what was proposed, not what's left
    return cleaned, cleaned.notes


def _validate(proposal: Proposal, dirs: list[tuple[Path, bool]],
              secrets: SecretSource | None, known: Described) -> Proposal:
    """`validate_proposal`'s checks on the stack and its additions, with
    `dirs` showing pending edits as if accepted."""
    missing = [b for b in proposal.bottles if find_bottle(b, dirs) is None]
    if missing:
        proposal = replace(proposal, bottles=[b for b in proposal.bottles if b not in missing],
                           notes=[*proposal.notes, *(f"dropped {b} from the stack: there's no "
                                                     f"such bottle" for b in missing)])
    proposal = make_runnable(proposal, dirs, secrets)
    notes = list(proposal.notes)
    add = proposal.additions

    good_egress = []
    for e in add.egress:
        err = _try_load_with_egress(proposal, [e], dirs)
        if err:
            notes.append(f"dropped egress route {e.host!r}: {err}")
        else:
            good_egress.append(e)

    unconnected: Described = {}
    good_repos = []
    for r in add.repos:
        host = grant_forge(r.grant)
        if host in known and host not in connected_hosts():
            unconnected[host] = known[host]
            good_repos.append(r)
            continue
        err = _try_load_with_repos(proposal, [r], dirs)
        if err:
            notes.append(f"dropped repo grant {r.grant!r}: {err}")
        else:
            good_repos.append(r)

    good_env = []
    for v in add.env:
        if SECRET_NAME.search(v.name):
            notes.append(f"dropped env {v.name!r}: a secret mustn't be an env var, which the "
                         f"agent can read; add it to the host secrets and propose it as a "
                         f"credential on the route that needs it")
        else:
            good_env.append(v)

    good_creds: list[ProposalCredential] = []
    valid_hosts = {e.host for e in good_egress}
    for c in add.credentials:
        if c.route not in valid_hosts:
            notes.append(
                f"dropped credential {c.name!r}: its route {c.route!r} was not accepted"
            )
        else:
            good_creds.append(c)

    cleaned = Proposal(
        option=proposal.option,
        bottles=proposal.bottles,
        new_bottle=proposal.new_bottle,
        additions=proposal.additions.__class__(
            egress=good_egress,
            repos=good_repos,
            env=good_env,
            credentials=good_creds,
        ),
        similar=proposal.similar,
        notes=notes,
        blocked=proposal.blocked,
        # The architect's sentence describes what it proposed; once anything
        # is dropped, the one made from what's left says it right.
        summary=proposal.summary if notes == proposal.notes else "",
        forges=unconnected,
    )
    return drop_covered(cleaned, dirs)


def is_stacked(proposal: Proposal) -> bool:
    """Whether the proposal stacks existing bottles (options 1-3) rather than
    making a new one."""
    return proposal.option not in (OPTION_NEW_EXTENDING, OPTION_NEW)


def stack_names(proposal: Proposal, additions: str) -> list[str]:
    """The bottles a launch with the proposal stacks, in merge order: its
    bottles, then `additions` (the bottle holding its additions), or for a
    new bottle just that one."""
    if is_stacked(proposal):
        return [*proposal.bottles, additions]
    return [additions]


def _try_load_with_egress(
    proposal: Proposal,
    extra_egress: list[ProposalEgress],
    dirs: list[tuple[Path, bool]],
) -> str:
    """Return an error string if loading a temporary bottle with `extra_egress`
    fails, else ""."""
    tmp_proposal = Proposal(
        option=proposal.option,
        bottles=proposal.bottles,
        new_bottle=proposal.new_bottle,
        additions=proposal.additions.__class__(
            egress=extra_egress,
            repos=[],
            env=[],
            credentials=[],
        ),
        similar=[],
        notes=[],
    )
    return _try_load_proposal(tmp_proposal, dirs)


def _try_load_with_repos(
    proposal: Proposal,
    extra_repos: list[Any],
    dirs: list[tuple[Path, bool]],
) -> str:
    tmp_proposal = Proposal(
        option=proposal.option,
        bottles=proposal.bottles,
        new_bottle=proposal.new_bottle,
        additions=ProposalAdditions(
            egress=[],
            repos=extra_repos,
            env=[],
            credentials=[],
        ),
        similar=[],
        notes=[],
    )
    return _try_load_proposal(tmp_proposal, dirs)


def _try_load_proposal(
    proposal: Proposal,
    dirs: list[tuple[Path, bool]],
) -> str:
    """Write the proposal to a temp file and load it; return "" on success, the
    error message on failure."""
    fm = proposal_to_frontmatter(proposal)
    if not fm:
        return ""
    text = f"---\n{serialize_yaml_subset(fm)}---\n\nTemporary validation bottle.\n"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        bottles_dir = tmp_dir / "bottles"
        bottles_dir.mkdir()
        (bottles_dir / "_suggest.md").write_text(text)
        tmp_dirs: list[tuple[Path, bool]] = [(bottles_dir, False)] + list(dirs)
        try:
            # Stacked as a launch stacks them (`merge_bottles_runtime`).
            merge_bottles_runtime([load_bottle_chain(n, tmp_dirs)[0]
                                   for n in stack_names(proposal, "_suggest")])
            return ""
        except ManifestError as e:
            return str(e).split("\n", 1)[0]


__all__ = [
    "is_stacked",
    "proposal_to_frontmatter",
    "stack_names",
    "validate_proposal",
]
