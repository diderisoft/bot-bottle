"""Bounds for `launch-bottle` (PRD 0106): running children count and depth."""

from __future__ import annotations

from typing import Any

DEFAULT_MAX_CHILDREN = 3


def running_children_of(parent_identity: str, bottles: Any) -> int:
    """Count running children of `parent_identity` (bottles with `parent` set
    to it in their metadata). `bottles` is a BottleService."""
    from ...bottle_state import bot_bottle_root, read_metadata
    state_root = bot_bottle_root() / "state"
    count = 0
    for meta_path in state_root.glob("*/metadata.json"):
        identity = meta_path.parent.name
        meta = read_metadata(identity)
        if meta is None or meta.parent != parent_identity:
            continue
        state = bottles.session_state(identity) if bottles is not None else {}
        if state.get("running"):
            count += 1
    return count


def is_launch_bottle_child(identity: str) -> bool:
    """Whether `identity` was itself launched by another bottle (depth = 1
    means its own `launch-bottle` calls are refused by default)."""
    from ...bottle_state import read_metadata
    meta = read_metadata(identity)
    return meta is not None and bool(meta.parent)


__all__ = ["DEFAULT_MAX_CHILDREN", "is_launch_bottle_child", "running_children_of"]
