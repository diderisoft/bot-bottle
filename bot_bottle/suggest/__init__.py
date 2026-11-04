"""Bottle suggester (PRD 0117): propose the bottle a task needs.

`suggest(task, cwd)` is the one flow the CLI and the console use
(`suggest.flow`):
- `bottle-architect`, the host's agent of that name, run with no tools;
- the rules path when it can't answer, or with `rules=True`: the project
  bottle if trusted, a write grant on `origin` when its forge is connected,
  else `basic`.

`Proposal` is the shared type.
"""

from __future__ import annotations

from pathlib import Path

from .proposal import (
    OPTION_NEW,
    OPTION_NEW_EXTENDING,
    OPTION_REUSE,
    OPTION_STACK,
    OPTION_STACK_ADDITIONS,
    Proposal,
    ProposalAdditions,
    ProposalCredential,
    ProposalEgress,
    ProposalEnv,
    ProposalNewBottle,
    ProposalRepo,
    ProposalSimilar,
)


def suggest(task: str, cwd: Path | None, *, rules: bool = False,
            host_root: Path | None = None) -> Proposal:
    """`suggest.flow.suggest`, imported when called so the package stays light."""
    from .flow import suggest as _suggest  # pylint: disable=import-outside-toplevel
    return _suggest(task, cwd, rules=rules, host_root=host_root)


__all__ = [
    "OPTION_NEW",
    "OPTION_NEW_EXTENDING",
    "OPTION_REUSE",
    "OPTION_STACK",
    "OPTION_STACK_ADDITIONS",
    "Proposal",
    "ProposalAdditions",
    "ProposalCredential",
    "ProposalEgress",
    "ProposalEnv",
    "ProposalNewBottle",
    "ProposalRepo",
    "ProposalSimilar",
    "suggest",
]
