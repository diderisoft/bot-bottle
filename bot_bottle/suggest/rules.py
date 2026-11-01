"""Rules-based bottle suggestion (PRD 0117, open-core path, step 1).

Stopping at the first that fits:
1. The project bottle (`./main`) if trusted.
2. A write grant on `origin` when its forge is connected.
3. Otherwise `basic`.

This is what `bot-bottle` (quick-launch) already does, expressed as a Proposal.
"""

from __future__ import annotations

from pathlib import Path

from ..manifest.loader import PROJECT_BOTTLE, bottle_ref, find_bottle, load_bottle_chain
from ..manifest.trust import UntrustedBottleError, check as trust_check
from ..manifest.util import ManifestError
from ..onboarding import find_forge, read_origin
from ..paths import bot_bottle_root
from .proposal import (
    OPTION_REUSE,
    OPTION_STACK_ADDITIONS,
    Proposal,
    ProposalAdditions,
    ProposalRepo,
)


def rules_suggest(
    task: str,
    cwd: Path | None = None,
    *,
    host_root: Path | None = None,
) -> Proposal:
    """Apply the three-rule path and return a Proposal.

    `cwd` is the project directory (defaults to the current directory).
    `host_root` is `~/.bot-bottle` (defaults to `bot_bottle_root()`).
    `task` doesn't affect the rules; it's taken so every path has one signature.
    """
    del task
    if cwd is None:
        import os
        cwd = Path(os.getcwd())
    if host_root is None:
        host_root = bot_bottle_root()

    host_bottles = host_root / "bottles"
    cwd_bottles = cwd / ".bot-bottle" / "bottles"

    dirs: list[tuple[Path, bool]] = [(host_bottles, False), (cwd_bottles, True)]
    from ..manifest.loader import BUILTIN_BOTTLES_DIR
    dirs.append((BUILTIN_BOTTLES_DIR, False))

    # Rule 1: project bottle ./main if trusted.
    project_bottle = bottle_ref(PROJECT_BOTTLE, True)
    if _is_trusted_project_bottle(project_bottle, dirs):
        return Proposal(
            option=OPTION_REUSE,
            bottles=[project_bottle],
            new_bottle=None,
            additions=ProposalAdditions(),
            similar=[],
            notes=[f"rules path: project bottle {project_bottle!r} is trusted"],
        )

    # Rule 2: write grant on origin when its forge is connected.
    origin = read_origin(cwd)
    if origin is not None:
        forge = find_forge(origin, host_bottles)
        if forge is not None:
            grant = f"{forge.key}/{origin.owner}/{origin.repo}:write"
            base = forge.bottle
            return Proposal(
                option=OPTION_STACK_ADDITIONS,
                bottles=[base, "basic"],
                new_bottle=None,
                additions=ProposalAdditions(
                    repos=[ProposalRepo(grant=grant,
                                        why="write access to the project's own repo")],
                ),
                similar=[],
                notes=[f"rules path: origin {origin.owner}/{origin.repo} on {forge.key}"],
            )

    # Rule 3: basic.
    return Proposal(
        option=OPTION_REUSE,
        bottles=["basic"],
        new_bottle=None,
        additions=ProposalAdditions(),
        similar=[],
        notes=["rules path: no project bottle or connected forge found; using basic"],
    )


def _is_trusted_project_bottle(
    bottle_name: str,
    dirs: list[tuple[Path, bool]],
) -> bool:
    """True when the project's bottle loads without a trust error."""
    found = find_bottle(bottle_name, dirs)
    if found is None:
        return False
    try:
        bottle, sources = load_bottle_chain(bottle_name, dirs)
        trust_check(bottle_name, bottle, sources)
        return True
    except (UntrustedBottleError, ManifestError):
        return False


__all__ = ["rules_suggest"]
