"""The CLI's bottle suggestions (PRD 0117): `POST /suggest` and `POST
/architect/warm`.

The suggestion runs in the host controller, where the architect's warm
bottle lives (`architect_bottle`), so `bot-bottle start`, `bot-bottle bottles
suggest` and the console all get the same flow.
"""

from __future__ import annotations

from pathlib import Path

from ...contract.hostctl import Settings
from .architect_bottle import current, runner
from .suggest_link import project_dir


def cwd_of(raw: object) -> Path | None:
    """The project directory the request names, or None when it names none
    or one the suggester mustn't read as a project (the home directory, or
    anywhere outside it): the proposal then says it had none."""
    try:
        return project_dir(raw)
    except ValueError:
        if raw is None or raw == "":
            return None
        if not isinstance(raw, str):
            raise
        return None


def suggest(task: str, cwd: Path | None, rules: bool, settings: Settings) -> dict[str, object]:
    """The job: the suggestion flow's validated proposal."""
    # pylint: disable-next=import-outside-toplevel
    from ...suggest.flow import suggest as flow_suggest
    proposal = flow_suggest(task, cwd, rules=rules, run=runner(settings))
    return {"proposal": proposal.to_dict()}


def warm(settings: Settings) -> dict[str, object]:
    """Boot the warm bottle in the background; its state."""
    architect = current()
    if architect is None:
        return {"state": "unavailable"}
    architect.warm(settings)
    return architect.status()


__all__ = ["cwd_of", "suggest", "warm"]
