"""Cleanup of per-bottle state dirs, for every backend.

A dir is an orphan when its bottle isn't live in any backend and it isn't
preserved. Only dirs whose recorded backend was listed successfully are
candidates, so a down runtime's state is left alone.
"""

from __future__ import annotations

import sys
from collections.abc import Collection
from dataclasses import dataclass

from ..bottle_state import bottle_state_dir, is_preserved, read_metadata
from ..contract.hostctl import Settings
from ..lib.log import info
from ..paths import bot_bottle_root
from .cleanup_control import CleanupFailures

# A state dir with no recorded backend is docker's.
_DEFAULT_BACKEND = "docker"


@dataclass(frozen=True)
class StateCleanupPlan:
    """Orphan state dirs to remove, by identity."""

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


def state_backend(identity: str) -> str:
    """The backend a state dir belongs to."""
    metadata = read_metadata(identity)
    return (metadata.backend if metadata is not None else "") or _DEFAULT_BACKEND


def orphan_state_dirs(live: Collection[str], listed: Collection[str]) -> list[str]:
    """State dirs whose bottle isn't `live`, that aren't preserved, and whose
    backend is in `listed`."""
    state_root = bot_bottle_root() / "state"
    if not state_root.is_dir():
        return []
    orphans: list[str] = []
    for child in sorted(state_root.iterdir()):
        identity = child.name
        if not child.is_dir() or identity in live or is_preserved(identity):
            continue
        if state_backend(identity) not in listed:
            continue
        orphans.append(identity)
    return orphans


def prepare_state_cleanup(live: Collection[str], listed: Collection[str]) -> StateCleanupPlan:
    return StateCleanupPlan(tuple(orphan_state_dirs(live, listed)))


def cleanup_state(plan: StateCleanupPlan, settings: Settings) -> None:
    failures = CleanupFailures(timeout=settings.operation.cleanup_command_timeout_seconds)
    for identity in plan.identities:
        path = bottle_state_dir(identity)
        info(f"removing orphan state dir {path}")
        failures.remove_tree(path, f"removing orphan state dir {path}")
    failures.raise_if_any()


__all__ = ["StateCleanupPlan", "cleanup_state", "orphan_state_dirs", "prepare_state_cleanup",
           "state_backend"]
