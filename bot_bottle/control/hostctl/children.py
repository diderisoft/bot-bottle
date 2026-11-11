"""Bounds for `launch-bottle` (PRD 0106): running children count and depth."""

from __future__ import annotations

from typing import Any

DEFAULT_MAX_CHILDREN = 3


def running_children_of(parent_identity: str, bottles: Any) -> int:
    """Count running children of `parent_identity` (bottles with `parent` set
    to it in their metadata). `bottles` is a BottleService."""
    from ...bottle_state import all_metadata
    count = 0
    for meta in all_metadata():
        if meta.parent != parent_identity:
            continue
        state = bottles.session_state(meta.identity) if bottles is not None else {}
        if state.get("running"):
            count += 1
    return count


def is_launch_bottle_child(identity: str) -> bool:
    """Whether `identity` was itself launched by another bottle (depth = 1
    means its own `launch-bottle` calls are refused by default)."""
    from ...bottle_state import read_metadata
    meta = read_metadata(identity)
    return meta is not None and bool(meta.parent)


def child_agent_ids(parent_bottle: str, running: "dict[str, str]") -> list[str]:
    """Agent IDs in `running` whose bottle metadata names `parent_bottle` as parent."""
    from ...bottle_state import read_metadata
    return [aid for aid, bid in list(running.items())
            if (m := read_metadata(bid)) and m.parent == parent_bottle]


__all__ = ["DEFAULT_MAX_CHILDREN", "child_agent_ids", "is_launch_bottle_child",
           "running_children_of"]
