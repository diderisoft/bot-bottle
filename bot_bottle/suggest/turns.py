"""The guided start's conversation with the architect (PRD 0118).

Each suggestion is a fresh headless call; a reply replays what came before:
each earlier proposal, what the user did with it (edits accepted or
skipped, forges connected) and what they said. The architect's bottle keeps
no session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

MAX_TURNS = 8
MAX_REPLY = 2000


@dataclass
class Turn:
    """One exchange: the `proposal` shown (its dict), what the user did with
    it (`events`) and their `reply`."""

    proposal: dict[str, Any]
    reply: str
    events: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"proposal": self.proposal, "reply": self.reply, "events": list(self.events)}


def parse_turns(raw: object) -> list[Turn]:
    """Turns from a request body. ValueError when they aren't turns."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_TURNS:
        raise ValueError(f"turns must be an array of at most {MAX_TURNS}")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each turn must be an object")
        proposal, reply, events = item.get("proposal"), item.get("reply"), item.get("events", [])
        if not isinstance(proposal, dict) or not isinstance(reply, str):
            raise ValueError("a turn has a proposal object and a reply string")
        if len(reply) > MAX_REPLY:
            raise ValueError(f"a reply is at most {MAX_REPLY} characters")
        if not isinstance(events, list) or not all(isinstance(e, str) for e in events):
            raise ValueError("a turn's events are strings")
        out.append(Turn(proposal, reply, list(events)))
    return out


def _shown(proposal: dict[str, Any]) -> dict[str, Any]:
    """What the architect proposed, without what the host added."""
    return {k: v for k, v in proposal.items() if k not in ("source", "blocked", "forges")}


def prompt_lines(turns: list[Turn]) -> list[str]:
    """The conversation so far, for the architect's prompt; none without turns."""
    if not turns:
        return []
    lines = ["", "The conversation so far. Each turn is your proposal as the person saw it "
                 "(after validation), what they did with it, and their reply. Answer the "
                 "last reply with a revised proposal. An accepted edit is already written: "
                 "the library above has it, so don't propose it again."]
    for n, turn in enumerate(turns, 1):
        lines += [f"Turn {n} proposal:", json.dumps(_shown(turn.proposal), indent=2)]
        if turn.events:
            lines.append(f"Turn {n}, what they did: " + "; ".join(turn.events))
        lines.append(f"Turn {n}, their reply: {turn.reply}")
    return lines


__all__ = ["MAX_REPLY", "MAX_TURNS", "Turn", "parse_turns", "prompt_lines"]
