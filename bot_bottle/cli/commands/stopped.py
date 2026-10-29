"""stopped: list bottles that aren't running but can be resumed: each state dir
with recorded metadata whose bottle no backend or the host controller has,
most recently used first.

    bot-bottle stopped
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from ...backend import enumerate_active_agents
from ...bottle_state import BottleMetadata, bottle_state_dir, is_preserved, read_metadata
from ...paths import bot_bottle_root
from ..constants import PROG
from ..settings import settings_from_env
from .active import held_bottles

_USAGE = f"""usage: {PROG} stopped

List bottles that aren't running and can be resumed, most recently used first.
Output: <identity>\\t<label>\\t<bottles>\\t<last used>\\t<state>
<state> is `kept` when the bottle's state was kept for resuming, or `left over`
when its session didn't end cleanly (`{PROG} cleanup` removes those).
`{PROG} resume <identity>` starts one again.
"""


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


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def cmd_stopped(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        sys.stderr.write(_USAGE)
        return 0
    live = {b.slug for b in enumerate_active_agents(settings_from_env())} | set(held_bottles())
    bottles = stopped_bottles(live)
    if not bottles:
        print("no stopped bot-bottle bottles", file=sys.stderr)
        return 0
    for metadata, used in bottles:
        agent = metadata.agent_name or "?"
        label = f"{metadata.label} ({agent})" if metadata.label else agent
        names = ",".join(metadata.bottle_names) or "-"
        state = "kept" if is_preserved(metadata.identity) else "left over"
        print(f"{metadata.identity}\t{label}\t{names}\t{_when(used)}\t{state}")
    sys.stdout.flush()  # the list, then the hint, even when piped
    print(f"\nresume one with `{PROG} resume <identity>`", file=sys.stderr)
    return 0


__all__ = ["cmd_stopped", "stopped_bottles"]
