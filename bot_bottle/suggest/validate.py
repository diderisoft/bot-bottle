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
from pathlib import Path
from typing import Any

from ..contract.yaml_subset import serialize_yaml_subset
from ..manifest.loader import BUILTIN_BOTTLES_DIR, load_bottle_chain
from ..manifest.util import ManifestError
from .proposal import (
    OPTION_NEW,
    OPTION_NEW_EXTENDING,
    Proposal,
    ProposalAdditions,
    ProposalCredential,
    ProposalEgress,
)


def _egress_routes(additions: ProposalAdditions) -> list[dict[str, Any]]:
    """Additions' egress entries as bottle frontmatter routes (no inspect:false)."""
    routes = []
    cred_by_host = {c.route: c for c in additions.credentials}
    for e in additions.egress:
        route: dict[str, Any] = {"host": e.host}
        inspect: dict[str, Any] = {}
        if e.methods:
            inspect["matches"] = [{"methods": list(e.methods)}]
        cred = cred_by_host.get(e.host)
        if cred:
            inspect["auth"] = {"scheme": "Bearer", "token_ref": cred.name}
        if inspect:
            route["inspect"] = inspect
        routes.append(route)
    return routes


def proposal_to_frontmatter(
    proposal: Proposal,
    forges: list[str] | None = None,
) -> dict[str, Any]:
    """A bottle frontmatter dict for the proposal's additions.

    For reuse/stack options this is an additions-only dict (to be layered on
    top of the stacked bottles via `extends`).  For new-extending / new it
    includes the `extends` from `new_bottle`.

    `forges` is a list of bottle names that declare connected forges; a repo
    grant on one becomes a one-line grant, which requires that bottle in the
    extends chain.
    """
    fm: dict[str, Any] = {}

    base_bottles = list(proposal.bottles)
    if proposal.option in (OPTION_NEW_EXTENDING, OPTION_NEW) and proposal.new_bottle:
        extends = proposal.new_bottle.extends or (base_bottles[0] if base_bottles else "basic")
        fm["extends"] = extends
    elif base_bottles:
        if len(base_bottles) == 1:
            fm["extends"] = base_bottles[0]
        else:
            fm["extends"] = base_bottles

    add = proposal.additions
    env: dict[str, str] = {}
    for v in add.env:
        env[v.name] = f"?{v.name}"  # prompt-at-runtime placeholder
    if env:
        fm["env"] = env

    repos = [r.grant for r in add.repos]
    if repos:
        fm["git-gate"] = {"repos": repos}

    routes = _egress_routes(add)
    if routes:
        fm["egress"] = {"routes": routes}

    return fm


def validate_proposal(
    proposal: Proposal,
    dirs: list[tuple[Path, bool]] | None = None,
) -> tuple[Proposal, list[str]]:
    """Validate the proposal by trying to load it with the manifest loader.

    Invalid additions are dropped individually (first egress routes, then repo
    grants, then env vars) and returned as notes.  The validated Proposal and
    accumulated notes are returned.

    `dirs` is the bottle search path as `(directory, is_project)` pairs (same
    as `ManifestIndex.bottle_dirs()`).  When None, only built-in bottles are
    available (sufficient for rules-path tests).
    """
    if dirs is None:
        dirs = [(BUILTIN_BOTTLES_DIR, False)]
    notes = list(proposal.notes)
    add = proposal.additions

    good_egress = []
    for e in add.egress:
        err = _try_load_with_egress(proposal, [e], dirs)
        if err:
            notes.append(f"dropped egress route {e.host!r}: {err}")
        else:
            good_egress.append(e)

    good_repos = []
    for r in add.repos:
        err = _try_load_with_repos(proposal, [r], dirs)
        if err:
            notes.append(f"dropped repo grant {r.grant!r}: {err}")
        else:
            good_repos.append(r)

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
            env=list(add.env),
            credentials=good_creds,
        ),
        similar=proposal.similar,
        notes=notes,
    )
    return cleaned, notes


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
    from .proposal import ProposalAdditions  # avoid circular at module level
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
            load_bottle_chain("_suggest", tmp_dirs)
            return ""
        except ManifestError as e:
            return str(e).split("\n", 1)[0]


__all__ = [
    "proposal_to_frontmatter",
    "validate_proposal",
]
