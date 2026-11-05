"""Drop proposed additions the proposal's own bottles already cover (PRD 0117).

An architect sometimes proposes an egress route, repo grant or env var that a
bottle it picked already has. Kept, such an addition isn't just noise: a later
bottle's route replaces an earlier one for the same host (ADR 0015), so a
`GET` addition would narrow `basic`'s `GET`/`HEAD` route. So an addition is
dropped when the bottles it stacks on already allow at least as much, and the
proposal says which bottle covered it.

"At least as much":
- egress: the base route has no credential the addition lacks, and allows
  every method the addition asks for on every path (a route with no `inspect`
  rules, or `inspect: false`, allows all). An addition that injects a
  credential is never dropped: it changes what the route does.
- repo grant: the base has the same repo, with write access when the addition
  asks for it.
- env var: the base sets the same name.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..manifest.bottle import ManifestBottle
from ..manifest.egress import ManifestEgressRoute
from ..manifest.extends import merge_bottles_runtime
from ..manifest.forges import parse_grant, split_grant_alias
from ..manifest.loader import load_bottle_chain
from ..manifest.util import ManifestError
from .proposal import (
    OPTION_NEW,
    OPTION_NEW_EXTENDING,
    OPTION_REUSE,
    OPTION_STACK,
    OPTION_STACK_ADDITIONS,
    Proposal,
    ProposalAdditions,
    ProposalEgress,
)

ALL_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE",
                         "CONNECT"})


def base_bottles(proposal: Proposal) -> list[str]:
    """The bottles the proposal's additions land on, in merge order."""
    if proposal.option in (OPTION_NEW_EXTENDING, OPTION_NEW):
        if proposal.new_bottle and proposal.new_bottle.extends:
            return [proposal.new_bottle.extends]
        return []
    return list(proposal.bottles)


def allowed_methods(route: ManifestEgressRoute) -> frozenset[str]:
    """The methods `route` allows on every path."""
    if not route.Inspect or not route.Matches:
        return ALL_METHODS
    allowed: set[str] = set()
    for match in route.Matches:
        if match.Paths or match.Headers:
            continue  # only for some paths or requests
        allowed |= set(match.Methods) if match.Methods else ALL_METHODS
    return frozenset(allowed)


def _egress_covered(addition: ProposalEgress, route: ManifestEgressRoute | None,
                    has_credential: bool) -> bool:
    if route is None or has_credential:
        return False
    wanted = {m.upper() for m in addition.methods} or ALL_METHODS
    return wanted <= allowed_methods(route)


def _repo_key(grant: str) -> tuple[str, str, bool] | None:
    """(owner, repo, writable) of a grant, or None when it doesn't parse."""
    try:
        _, owner, repo, writable = parse_grant(split_grant_alias(grant)[1], "proposal")
    except ManifestError:
        return None
    return owner, repo, writable


def repo_covered(grant: str, base_repos: dict[tuple[str, str], bool]) -> bool:
    key = _repo_key(grant)
    if key is None:
        return False
    owner, repo, writable = key
    base_writable = base_repos.get((owner, repo))
    return base_writable is not None and (base_writable or not writable)


def _loaded(names: list[str], dirs: list[tuple[Path, bool]]) -> ManifestBottle | None:
    """The bottles `names` merged, or None when one doesn't load."""
    try:
        return merge_bottles_runtime([load_bottle_chain(n, dirs)[0] for n in names])
    except (ManifestError, ValueError):
        return None


def _base_repos(bottle: ManifestBottle) -> dict[tuple[str, str], bool]:
    """{(owner, repo): writable} for the bottle's git-gate repos."""
    out: dict[tuple[str, str], bool] = {}
    for entry in bottle.git:
        path = entry.UpstreamPath or entry.Upstream
        parts = path.rstrip("/").removesuffix(".git").split("/")
        if len(parts) >= 2:
            out[(parts[-2], parts[-1])] = not entry.Key.read_only
    return out


def drop_covered(proposal: Proposal, dirs: list[tuple[Path, bool]]) -> Proposal:
    """`proposal` without the additions its base bottles already cover, with
    one note per bottle saying how many it covered. Unchanged when it has no
    base, or a base bottle doesn't load (validation reports that)."""
    names = base_bottles(proposal)
    if not names or proposal.additions.is_empty():
        return proposal
    merged = _loaded(names, dirs)
    if merged is None:
        return proposal
    singles = {n: _loaded([n], dirs) for n in names}

    def covering(test: Callable[[ManifestBottle], bool]) -> str:
        """The first base bottle that covers on its own, else the stack."""
        for n in names:
            b = singles.get(n)
            if b is not None and test(b):
                return n
        return " + ".join(names)

    add = proposal.additions
    cred_hosts = {c.route for c in add.credentials}
    dropped: dict[str, list[str]] = {}

    def routes(bottle: ManifestBottle) -> dict[str, ManifestEgressRoute]:
        return {r.Host.lower(): r for r in bottle.egress.routes}

    merged_routes = routes(merged)
    keep_egress = []
    for e in add.egress:
        has_cred = e.host in cred_hosts
        if _egress_covered(e, merged_routes.get(e.host.lower()), has_cred):
            who = covering(lambda b, e=e, c=has_cred:
                           _egress_covered(e, routes(b).get(e.host.lower()), c))
            dropped.setdefault(who, []).append(f"egress {e.host}")
        else:
            keep_egress.append(e)

    merged_repos = _base_repos(merged)
    keep_repos = []
    for r in add.repos:
        if repo_covered(r.grant, merged_repos):
            who = covering(lambda b, r=r: repo_covered(r.grant, _base_repos(b)))
            dropped.setdefault(who, []).append(f"repo {r.grant}")
        else:
            keep_repos.append(r)

    keep_env = []
    for v in add.env:
        if v.name in merged.env:
            who = covering(lambda b, v=v: v.name in b.env)
            dropped.setdefault(who, []).append(f"env {v.name}")
        else:
            keep_env.append(v)

    if not dropped:
        return proposal
    notes = list(proposal.notes)
    for who, items in dropped.items():
        notes.append(f"dropped {len(items)} addition{'s' if len(items) != 1 else ''} "
                     f"already covered by {who}: {', '.join(items)}")
    proposal.additions = ProposalAdditions(egress=keep_egress, repos=keep_repos, env=keep_env,
                                           credentials=list(add.credentials))
    proposal.notes = notes
    if proposal.option == OPTION_STACK_ADDITIONS and proposal.additions.is_empty():
        proposal.option = OPTION_STACK if len(proposal.bottles) > 1 else OPTION_REUSE
    return proposal


__all__ = ["ALL_METHODS", "allowed_methods", "base_bottles", "drop_covered", "repo_covered"]
