"""Bottle suggester (PRD 0117): propose the bottle a task needs.

Two paths:
- Rules: the project bottle if trusted, a write grant on `origin` when its
  forge is connected, else `basic`.
- `bottle-architect`: the user's own harness in print mode with no tools,
  validated typed JSON.

`Proposal` is the shared type; later chunks (replay, host link, hosted
suggester) reuse it.
"""

from __future__ import annotations

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
]
