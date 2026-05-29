"""Shared print helpers for BottlePlan.print implementations.

Lifts the multi-value label printer out of DockerBottlePlan so the
smolmachines backend (and any future backend) renders the same
two-column scannable preflight without duplicating the indent
math."""

from __future__ import annotations

from typing import Sequence

from ..agent_provider import runtime_for
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
    env_names: Sequence[str], *, agent_provider_template: str,
) -> list[str]:
    """Env names worth showing in launch summaries.

    Provider auth placeholders (currently `CLAUDE_CODE_OAUTH_TOKEN`)
    are implementation details: they are non-secret dummy values that
    satisfy the provider CLI while egress injects the real upstream
    Authorization header. Showing them in preflight makes the operator
    think a real key is entering the agent, so hide only the active
    provider-owned placeholder.
    """
    hidden = {runtime_for(agent_provider_template).placeholder_env}
    return sorted({name for name in env_names if name and name not in hidden})
