"""The CLI's bottle suggestions (PRD 0117), asked of the host controller.

The architect runs in its own bottle, which the host controller keeps warm,
so `suggest` sends the task there (`POST /suggest`) and `warm` boots the
bottle early (`POST /architect/warm`, from `bot-bottle start`, so the boot
overlaps the harness picker and the task prompt). `rules=True` stays local:
the offline, deterministic path needs no model and no host controller.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..contract.hostctl import ARCHITECT_WARM_PATH, SUGGEST_PATH, Settings
from ..control.hostctl import runner
from ..control.hostctl.client import HostControlClient
from ..control.hostctl.runner import HostControlError
from ..suggest.proposal import Proposal

if TYPE_CHECKING:
    from ..suggest.turns import Turn

NO_HOST_NOTE = "bottle-architect didn't answer (the host controller isn't reachable: {}); " \
               "this is the rules path"


def _client() -> HostControlClient:
    return HostControlClient(runner.ensure_running(), runner.cli_token())


def suggest(task: str, cwd: Path | None, *, settings: Settings,
            rules: bool = False, turns: "list[Turn] | None" = None) -> Proposal:
    """The suggestion flow's validated proposal for `task`; `turns`, the
    conversation with the architect so far (PRD 0118)."""
    # pylint: disable-next=import-outside-toplevel
    from ..suggest.flow import suggest as local_suggest
    if rules:
        return local_suggest(task, cwd, rules=True)
    try:
        client = _client()
        job = str(client.post(SUGGEST_PATH, {
            "task": task, "cwd": str(cwd) if cwd else "", "rules": False,
            "settings": settings.to_json(),
            **({"turns": [t.to_dict() for t in turns]} if turns else {})})["job"])
        final = client.follow(job, lambda _line: None)
    except (HostControlError, OSError) as e:
        return _local_fallback(task, cwd, str(e))
    if final is None or final.get("type") != "result":
        message = "interrupted" if final is None else str(final.get("message") or "it failed")
        return _local_fallback(task, cwd, message)
    value = final.get("value")
    raw = value.get("proposal") if isinstance(value, dict) else None
    proposal = Proposal.from_dict(raw)
    proposal.source = str(raw.get("source", "")) if isinstance(raw, dict) else ""
    proposal.blocked = str(raw.get("blocked", "")) if isinstance(raw, dict) else ""
    return proposal


def _local_fallback(task: str, cwd: Path | None, why: str) -> Proposal:
    # pylint: disable-next=import-outside-toplevel
    from ..suggest.flow import suggest as local_suggest
    proposal = local_suggest(task, cwd, rules=True)
    proposal.notes.insert(0, NO_HOST_NOTE.format(why))
    return proposal


def warm(settings: Settings) -> str:
    """Boot the architect's bottle in the background; never fails a caller.
    Its state (`ready`, `booting`, …), or "" when the host didn't say."""
    try:
        reply = _client().post(ARCHITECT_WARM_PATH, {"settings": settings.to_json()})
    except (HostControlError, OSError):
        return ""
    state = reply.get("state")
    return state if isinstance(state, str) else ""


__all__ = ["NO_HOST_NOTE", "suggest", "warm"]
