"""A bottle suggestion for the console's launch page (PRD 0117), over the link.

`bottle_suggest` {task} starts a job and answers at once: the suggester (the
rules path, then `bottle-architect` under the host's login, as
`bot-bottle bottles suggest --architect` runs it) can take a minute, and the
link mustn't wait on it. `bottle_suggest_status` {job} follows it to the
proposal, which the host keeps. `bottle_suggestion_accept` {job, name?}
writes the bottle it proposes, if it's new, into the host's bottles and
answers the bottles to launch with. The console never sends a manifest: what
gets written is what this host proposed. Secret values never leave the host;
only their names are in a proposal."""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...lib import log
from ...suggest.proposal import Proposal

MAX_TASK_CHARS = 4000
MAX_JOBS = 4
# A job still running after this is reported as failed.
RUN_SECONDS = 120.0
# A finished job is kept this long for its status and acceptance.
KEEP_SECONDS = 600.0


@dataclass
class _Job:
    task: str
    started: float
    state: str = "running"  # running | done | error
    proposal: Proposal | None = None
    error: str = ""
    accepted: list[str] = field(default_factory=list)


_jobs: dict[str, _Job] = {}
_lock = threading.Lock()


def _suggest(task: str, cwd: Path) -> Proposal:
    """`bottles suggest --architect`: the architect (which falls back to the
    rules path), then validated against this host's bottles."""
    # pylint: disable=import-outside-toplevel
    from ...manifest import ManifestIndex
    from ...manifest.loader import BUILTIN_BOTTLES_DIR
    from ...suggest.architect import architect_suggest
    from ...suggest.validate import validate_proposal
    index = ManifestIndex.resolve(str(cwd), missing_ok=True)
    dirs = index.bottle_dirs() if index.home_md else [(BUILTIN_BOTTLES_DIR, False)]
    proposal = architect_suggest(task, cwd)
    return validate_proposal(proposal, dirs)[0]


def _expire(now: float) -> None:
    for job_id in [j for j, job in _jobs.items()
                   if job.state != "running" and now - job.started > KEEP_SECONDS]:
        del _jobs[job_id]


def start(task: str, *, cwd: Path | None = None,
          suggest: Callable[[str, Path], Proposal] = _suggest,
          clock: Callable[[], float] = time.monotonic) -> str:
    """Start a suggestion for `task` and return its job id. ValueError if the
    task isn't usable or too many are running."""
    task = task.strip()
    if not task or len(task) > MAX_TASK_CHARS:
        raise ValueError(f"the task must be text of 1 to {MAX_TASK_CHARS} characters")
    now = clock()
    with _lock:
        _expire(now)
        if sum(1 for j in _jobs.values() if j.state == "running") >= MAX_JOBS:
            raise ValueError("too many suggestions are running on this host; try again soon")
        job_id = secrets.token_hex(8)
        job = _jobs[job_id] = _Job(task=task, started=now)

    def run() -> None:
        try:
            proposal = suggest(task, cwd or Path.home())
        except Exception as e:  # pylint: disable=broad-exception-caught  # reported to the console
            log.warn(f"remote: suggesting a bottle failed: {type(e).__name__}: {e}")
            with _lock:
                job.state, job.error = "error", "suggesting a bottle failed; see the host's log"
            return
        with _lock:
            job.state, job.proposal = "done", proposal

    threading.Thread(target=run, daemon=True, name=f"suggest-{job_id}").start()
    return job_id


def summary(proposal: Proposal) -> dict[str, Any]:
    """What the console shows of a proposal: its kind, the bottles it stacks
    or creates, and the names (never values) of what it adds."""
    # pylint: disable-next=import-outside-toplevel
    from ...suggest.validate import is_stacked
    add = proposal.additions
    new = proposal.new_bottle
    return {
        "option": proposal.option,
        "bottles": list(proposal.bottles),
        "new_bottle": ({"name": new.name, "extends": new.extends, "about": new.about}
                       if new else None),
        "stacked": is_stacked(proposal),
        "writes": not is_stacked(proposal) or not add.is_empty(),
        "egress": [e.host for e in add.egress],
        "repos": [r.grant for r in add.repos],
        "env": [v.name for v in add.env],
        "credentials": [{"name": c.name, "route": c.route} for c in add.credentials],
        "similar": [s.bottle for s in proposal.similar],
        "notes": list(proposal.notes),
    }


def status(job_id: str, *, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """A job's state, and its proposal's summary once it's done."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return {"state": "error", "error": "no such suggestion (it may have expired)"}
        if job.state == "running" and clock() - job.started > RUN_SECONDS:
            job.state, job.error = "error", "suggesting a bottle took too long"
        out: dict[str, Any] = {"state": job.state}
        if job.error:
            out["error"] = job.error
        if job.proposal is not None:
            out["proposal"] = summary(job.proposal)
        return out


def accept(job_id: str, name: str = "", *,
           write: Callable[[str, Proposal], Path] | None = None) -> dict[str, Any]:
    """Accept a job's proposal: write its bottle if it makes one (named `name`,
    else the name it proposed) and answer the bottles to launch with."""
    # pylint: disable=import-outside-toplevel
    from ...suggest.validate import is_stacked, stack_names
    from ...suggest.write import SuggestWriteError, write_bottle
    with _lock:
        job = _jobs.get(job_id)
        proposal = job.proposal if job is not None and job.state == "done" else None
        if job is not None and job.accepted:
            return {"bottles": list(job.accepted)}
    if job is None or proposal is None:
        return {"error": "no finished suggestion to use (it may have expired)"}
    if is_stacked(proposal) and proposal.additions.is_empty():
        bottles = list(proposal.bottles) or ["basic"]
        written = ""
    else:
        written = name.strip() or (proposal.new_bottle.name if proposal.new_bottle else "")
        if not written:
            return {"error": "the bottle needs a name"}
        try:
            (write or write_bottle)(written, proposal)
        except SuggestWriteError as e:
            return {"error": str(e)}
        bottles = stack_names(proposal, written)
    with _lock:
        job.accepted = bottles
    return {"bottles": bottles, **({"written": written} if written else {})}


# --- the link's requests (remote_actions.HOST_REQUESTS) ---


def _text(msg: Mapping[str, Any], key: str) -> str:
    value = msg.get(key)
    return value if isinstance(value, str) else ""


async def bottle_suggest(msg: Mapping[str, Any]) -> dict[str, Any]:
    reply: dict[str, Any] = {"type": "bottle_suggestion", "request_id": msg.get("request_id")}
    try:
        job_id = await asyncio.to_thread(start, _text(msg, "task"))
    except ValueError as e:
        reply["error"] = str(e)
        return reply
    reply.update(job=job_id, state="running")
    return reply


async def bottle_suggest_status(msg: Mapping[str, Any]) -> dict[str, Any]:
    reply: dict[str, Any] = {"type": "bottle_suggestion", "request_id": msg.get("request_id"),
                             "job": _text(msg, "job")}
    reply.update(status(_text(msg, "job")))
    return reply


async def bottle_suggestion_accept(msg: Mapping[str, Any]) -> dict[str, Any]:
    reply: dict[str, Any] = {"type": "bottle_suggestion_accepted",
                             "request_id": msg.get("request_id"), "job": _text(msg, "job")}
    reply.update(await asyncio.to_thread(accept, _text(msg, "job"), _text(msg, "name")))
    return reply


REQUESTS = {"bottle_suggest": bottle_suggest, "bottle_suggest_status": bottle_suggest_status,
            "bottle_suggestion_accept": bottle_suggestion_accept}

__all__ = ["MAX_JOBS", "MAX_TASK_CHARS", "REQUESTS", "accept", "bottle_suggest",
           "bottle_suggest_status", "bottle_suggestion_accept", "start", "status", "summary"]
