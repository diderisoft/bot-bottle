"""stopped: the old name for `bots --stopped` (ADR 0009), and finding the
stopped bottled agents: each state dir with recorded metadata whose bottled
agent no backend or the host controller has, most recently used first."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from ...bottle_state import BottleMetadata, bottle_state_dir, read_metadata
from ...paths import bot_bottle_root
from ..constants import PROG


def _last_used(path: Path) -> float:
    """When the state dir last changed: its own mtime or its newest entry's."""
    times = [path.stat().st_mtime]
    for child in path.iterdir():
        try:
            times.append(child.stat().st_mtime)
        except OSError:
            continue
    return max(times)


def stopped_bottles(live: set[str]) -> list[tuple[BottleMetadata, float]]:
    """Resumable bottles not in `live`, with when each was last used, newest
    first."""
    state_root = bot_bottle_root() / "state"
    if not state_root.is_dir():
        return []
    found: list[tuple[BottleMetadata, float]] = []
    for child in state_root.iterdir():
        if not child.is_dir() or child.name in live:
            continue
        metadata = read_metadata(child.name)
        if metadata is None:
            continue
        try:
            found.append((metadata, _last_used(bottle_state_dir(child.name))))
        except OSError:
            continue
    return sorted(found, key=lambda m: m[1], reverse=True)


def when(ts: float) -> str:
    """A timestamp as local `YYYY-MM-DD HH:MM`."""
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def cmd_stopped(argv: list[str]) -> int:
    """The old name for `bots --stopped`, kept for a release."""
    # pylint: disable-next=import-outside-toplevel
    from .bottled_agents import cmd_bottled_agents
    if not argv:
        print(f"note: `{PROG} stopped` is now `{PROG} bots --stopped`", file=sys.stderr)
    return cmd_bottled_agents(argv or ["--stopped"])


__all__ = ["cmd_stopped", "stopped_bottles", "when"]
