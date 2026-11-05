"""What the guided `bot-bottle start` remembers for you: the agent (harness)
you last ran, in `~/.bot-bottle/start.json`. Missing or unreadable, nothing
is remembered and `start` asks."""

from __future__ import annotations

import json
from pathlib import Path

from ..paths import bot_bottle_root

PREFS_FILE = "start.json"


def _path() -> Path:
    return bot_bottle_root() / PREFS_FILE


def _read() -> dict[str, object]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remembered_agent() -> str:
    """The agent the guided start last ran, or ""."""
    agent = _read().get("agent")
    return agent if isinstance(agent, str) else ""


def remember_agent(agent: str) -> None:
    """Make `agent` the guided start's default; never fails the launch."""
    data = {**_read(), "agent": agent}
    try:
        _path().parent.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


__all__ = ["PREFS_FILE", "remember_agent", "remembered_agent"]
