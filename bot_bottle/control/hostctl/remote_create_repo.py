"""An agent's `create-repo` on the console (PRD 0120): its card, and the
console's Create answered here with what happened.

The card is ([], []) when no one needs to see it: refused (the forge isn't
connected, the repo exists), or created at once because the bottle allows
creating there (`git-gate.actions.create`). Through the GitHub App, a card
whose owner lacks the app (or its Administration permission) carries
`install:`, and `remote_actions.card_changed` raises it again once that's
fixed. Its option is `create_repo`: the console's Create posts
`create_repo_answer`, and the reply says what was created or why not.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

from ...lib import log

OPTION = "create_repo"


def create_now(client: Any, request: Any, bottles: Any, via: str) -> tuple[bool, str]:
    """Create and grant the repo a `create-repo` asked for, and answer it:
    (created, the note the agent reads). ValueError, unanswered, while the
    GitHub App isn't ready for its owner: the card stays."""
    # pylint: disable=import-outside-toplevel
    from .nudge import nudge_if_waited
    from .repo_create import AppNotReady, CreateRepoError, create_and_grant, plan
    try:
        new, forge = plan(request.session, request.proposed)
        note = create_and_grant(request.session, new, forge,
                                getattr(bottles, "grant_repo", None))
    except AppNotReady as e:
        raise ValueError(f"{e.line} (still waiting)") from e
    except CreateRepoError as e:
        client.supervise_respond(request.id, bottle_slug=request.bottle, decision="reject",
                                 notes=f"{e}, from {via}")
        nudge_if_waited(request, "denied", str(e))
        return False, f"refused create-repo: {e}"
    client.supervise_respond(request.id, bottle_slug=request.bottle, decision="approve",
                             notes=f"{note}, from {via}")
    nudge_if_waited(request, "approved", note)
    return True, note


def _answered_here(client: Any, request: Any, bottles: Any, via: str) -> bool:
    """Answer it without a card; False when that failed (the card shows)."""
    # pylint: disable-next=import-outside-toplevel
    from ..client import AlreadyAnswered
    try:
        log.info(f"remote: {request.session}: {create_now(client, request, bottles, via)[1]}")
    except AlreadyAnswered:
        pass
    except Exception as e:  # pylint: disable=broad-exception-caught  # the card instead
        log.warn(f"remote: answering create-repo failed: {type(e).__name__}: {e}")
        return False
    return True


def card(request: Any, orchestrator: Any, bottles: Any) -> tuple[list[str], list[str]]:
    """A `create-repo`'s card, or ([], []) when it's answered without one."""
    # pylint: disable=import-outside-toplevel
    from . import remote_actions
    from .repo_create import AppNotReady, CreateRepoError, allowed_by_bottle, card_lines, plan
    client = orchestrator(request.orchestrator)
    try:
        new, forge = plan(request.session, request.proposed)
    except AppNotReady as e:
        fix = [f"install: {e.url}", e.line]
        now = time.monotonic()
        # pylint: disable-next=protected-access
        remote_actions._unreached.setdefault(request.id, [fix, now, now])[0] = fix
        why = request.justification.strip()
        return [f"create: {request.proposed.strip()}", *fix,
                *([f"why: {why}"] if why else [])], [OPTION]
    except CreateRepoError:
        if _answered_here(client, request, None, "this host"):
            return [], []
        return [f"create: {request.proposed.strip()}"], []
    if allowed_by_bottle(request.session, new) and _answered_here(
            client, request, bottles, "the bottle (actions.create)"):
        return [], []
    return card_lines(request, new, forge), [OPTION]


async def create_repo_answer(msg: Mapping[str, Any], asking: Mapping[str, Any],
                             orchestrator: Any, bottles: Any) -> dict[str, Any]:
    """The console's Create on a `create-repo` card: `message` when it was
    created and granted (or refused, with `refused`), `error` when it wasn't
    answered (still waiting, or no longer waiting)."""
    # pylint: disable=import-outside-toplevel
    from ...contract.supervise import TOOL_CREATE_REPO
    from ...util import same_instant
    from ..client import AlreadyAnswered
    agent_id = str(msg.get("agent_id", ""))
    reply: dict[str, Any] = {"type": "create_repo_answered",
                             "request_id": msg.get("request_id"), "agent_id": agent_id}
    request = asking.get(agent_id)
    since = msg.get("since")
    if (request is None or request.tool != TOOL_CREATE_REPO
            or not same_instant(since if isinstance(since, str) else "", request.arrived)):
        reply["error"] = "That request is no longer waiting."
        return reply
    try:
        created, note = await asyncio.to_thread(
            create_now, orchestrator(request.orchestrator), request, bottles, "the phone")
    except ValueError as e:
        reply["error"] = str(e)
        return reply
    except AlreadyAnswered:
        reply["error"] = "It was answered elsewhere meanwhile."
        return reply
    except Exception as e:  # pylint: disable=broad-exception-caught  # the operator's told
        log.warn(f"remote: creating for {request.session} failed: {type(e).__name__}: {e}")
        reply["error"] = "Creating it failed; see the host controller's log."
        return reply
    log.info(f"remote: {request.session}: {note}")
    reply["message"] = note
    if not created:
        reply["refused"] = True
    return reply


__all__ = ["OPTION", "card", "create_now", "create_repo_answer"]
