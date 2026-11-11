"""Cleanup of left-over bottled agents' state, for every backend.

A bottled agent is an orphan when its bottle isn't live in any backend and it
isn't preserved. Only those whose recorded backend was listed successfully
are candidates, so a down runtime's state is left alone; one that records no
backend is always a candidate. A state dir no row records is an orphan too
unless its bottle is live: without a row it can't be resumed. Removing one
deletes its state dir and its host DB rows together (`remove_bottled_agent`).
"""

from __future__ import annotations

import sys
from collections.abc import Collection
from dataclasses import dataclass

from ..bottle_state import all_metadata, is_preserved, remove_bottled_agent, state_root
from ..contract.hostctl import Settings
from ..lib.log import info
from .cleanup_control import CleanupFailures


@dataclass(frozen=True)
class StateCleanupPlan:
    """Orphan bottled agents to remove, by identity."""

    identities: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.identities

    def intersect(self, current: "StateCleanupPlan") -> "StateCleanupPlan":
        return StateCleanupPlan(tuple(x for x in self.identities if x in current.identities))

    def print(self) -> None:
        print(file=sys.stderr)
        for identity in self.identities:
            info(f"orphan state:    {identity}")
        print(file=sys.stderr)


def orphans(live: Collection[str], listed: Collection[str]) -> list[str]:
    """What to remove: recorded bottled agents whose bottle isn't `live`,
    that aren't preserved, and whose backend is in `listed` (or that record
    none, so no runtime holds them), and every state dir no row records and
    no bottle is `live` under, preserved or not: nothing can resume it."""
    if not listed:
        return []
    recorded = all_metadata()
    found = {m.identity for m in recorded
             if m.identity not in live and not is_preserved(m.identity)
             and (m.backend in listed or not m.backend)}
    states = state_root()
    if states.is_dir():
        known = {m.identity for m in recorded}
        found.update(child.name for child in states.iterdir()
                     if child.is_dir() and child.name not in known and child.name not in live)
    return sorted(found)


def prepare_state_cleanup(live: Collection[str], listed: Collection[str]) -> StateCleanupPlan:
    return StateCleanupPlan(tuple(orphans(live, listed)))


def cleanup_state(plan: StateCleanupPlan, settings: Settings) -> None:
    failures = CleanupFailures(timeout=settings.operation.cleanup_command_timeout_seconds)
    for identity in plan.identities:
        info(f"removing orphan bottled agent {identity}")
        try:
            remove_bottled_agent(identity)
        except Exception as exc:  # pylint: disable=broad-exception-caught  # report the rest too
            failures.record(f"removing orphan bottled agent {identity}: {exc}")
    failures.raise_if_any()


__all__ = ["StateCleanupPlan", "cleanup_state", "orphans", "prepare_state_cleanup"]
