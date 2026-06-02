"""Shared print helpers for BottlePlan.print implementations.

Lifts the multi-value label printer out of DockerBottlePlan so the
smolmachines backend (and any future backend) renders the same
two-column scannable preflight without duplicating the indent
math."""

from __future__ import annotations

from typing import Sequence

from ..log import info


def print_multi(label: str, values: Sequence[str]) -> None:
    """Print `label: <value>` with continuation lines indented to
    align under the first value. Empty `values` renders `(none)`.

    Used by every backend's `BottlePlan.print` for env / skills /
    git / egress — one item per line keeps the preflight summary
    scannable when an agent has many of any of these."""
    if not values:
        info(f"{label}: (none)")
        return
    info(f"{label}: {values[0]}")
    indent = " " * (len(label) + 2)
    for v in values[1:]:
        info(f"{indent}{v}")


def visible_agent_env_names(
    env_names: Sequence[str], *, hidden_env_names: frozenset[str],
) -> list[str]:
    """Env names worth showing in launch summaries.

    Provider-injected placeholder env vars are implementation details:
    they are non-secret dummy values that satisfy provider CLIs while
    egress injects the real Authorization header. The plan's
    `hidden_env_names` carries exactly which names to suppress.
    """
    return sorted({name for name in env_names if name and name not in hidden_env_names})
