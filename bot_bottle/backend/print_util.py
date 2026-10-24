"""Print helpers shared by `BottlePlan.print` implementations."""

from __future__ import annotations

from typing import Sequence

from ..lib.log import info


def print_multi(label: str, values: Sequence[str]) -> None:
    """Print `label: <value>` with continuation lines aligned under the first
    value; `(none)` when empty."""
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
    """Env names to show in launch summaries, minus the provider's placeholder
    names (`hidden_env_names`).
    """
    return sorted({name for name in env_names if name and name not in hidden_env_names})
