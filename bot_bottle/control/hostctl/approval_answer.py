"""The console's plain Allow/Deny (`approval`) on a raised request, and what to
tell it back. A console of link protocol 2 sends a `request_id` and shows the
`approval_answered` reply on the card (ADR 0024)."""

from __future__ import annotations

from typing import Any, Callable

from ...contract.console_link import CARD_ANSWERS
from ...lib import log
from . import remote_actions

# The console's decisions -> approvals'.
DECISIONS = {"allow": "allow_once", "allow_project": "allow_project", "deny": "deny"}


def reply(request_id: object, agent_id: str, *, message: str = "",
          error: str = "") -> dict[str, Any] | None:
    """The `approval_answered` reply, or None for a console that sent no
    `request_id` (protocol 1), which expects none."""
    if request_id is None:
        return None
    return {"type": "approval_answered", "request_id": request_id, "agent_id": agent_id,
            **({"error": error} if error else {"message": message})}


async def answer(request: Any, decision: str, orchestrator: Callable[[str], Any],
                 bottles: Any) -> tuple[str, str]:
    """Apply the console's `decision` ("allow", "allow_project" or "deny") to
    `request`: (what happened, "") once applied, or ("", why not) when the
    request is still waiting. Logged either way."""
    # pylint: disable=import-outside-toplevel
    from ...contract.supervise import TOOL_ADD_REPO, TOOL_LAUNCH_BOTTLE
    from ..client import AlreadyAnswered
    chosen = DECISIONS[decision]
    if request.tool in (TOOL_ADD_REPO, TOOL_LAUNCH_BOTTLE) and decision != "deny":
        # Its card has its own answer (CARD_ANSWERS); a plain allow is from a
        # console that predates it.
        own = CARD_ANSWERS[request.tool.replace("-", "_")]
        log.warn(f"remote: the console answered {request.session}'s {request.tool} request "
                 f"with a plain {decision!r}, not {own!r}; it may need updating")
        return "", (f"This {request.tool} request takes its card's own answer, not a plain "
                    "Allow; the console may need updating.")
    try:
        message = await remote_actions.answer(orchestrator(request.orchestrator), request,
                                              chosen, bottles, via="the phone")
    except AlreadyAnswered as e:
        # Answered in the terminal first; it stands. The push clears the phone's.
        log.info(f"remote: {request.session}: already answered ({e.decided_as}); "
                 "the phone's answer wasn't applied")
        return f"already answered ({e.decided_as}) on the host", ""
    except Exception as e:  # pylint: disable=broad-exception-caught  # logged; still pending
        log.warn(f"remote: couldn't {decision} {request.session}'s {request.tool} "
                 f"request: {e}")
        return "", f"Couldn't {decision.replace('_', ' ')} it: {e}"
    log.info(f"remote: {request.session}: {message}")
    return message, ""


__all__ = ["DECISIONS", "answer", "reply"]
