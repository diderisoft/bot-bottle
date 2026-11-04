"""A bottle suggestion for the console's launch page (PRD 0117), over the link.

`bottle_suggest` {task, cwd?} starts a job and answers at once: the suggester
(`suggest.flow`, the same flow `bot-bottle bottles suggest` runs:
`bottle-architect` under the host's login, the rules path when it can't
answer) can take a minute, and the link mustn't wait on it. `cwd` is the
project directory on this host, when the console has one; without it the
proposal says there was no project to look at. `bottle_suggest_status` {job} follows it to the
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
# A job still running after this is reported as failed: past the
# architect's own timeout, so its rules fallback still gets to answer.
RUN_SECONDS = 150.0
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


def _suggest(task: str, cwd: Path | None) -> Proposal:
    """The shared suggestion flow, as `bot-bottle bottles suggest` runs it."""
    from ...suggest.flow import suggest  # pylint: disable=import-outside-toplevel
    return suggest(task, cwd)


def project_dir(raw: object, *, home: Path | None = None) -> Path | None:
    """The project directory a request names, or None when it names none.
    ValueError unless it's an existing directory under the user's home (and
    not the home itself, whose `.bot-bottle` is the host's, not a project's)."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or not raw.startswith("/"):
        raise ValueError("cwd must be an absolute path on this host")
    home = (home or Path.home()).resolve()
    path = Path(raw).resolve()
    if path == home or home not in path.parents:
        raise ValueError("cwd must be a project directory under your home on this host")
    if not path.is_dir():
        raise ValueError(f"cwd {raw!r} isn't a directory on this host")
    return path


def _expire(now: float) -> None:
    for job_id in [j for j, job in _jobs.items()
                   if job.state != "running" and now - job.started > KEEP_SECONDS]:
        del _jobs[job_id]


def start(task: str, *, cwd: Path | None = None,
          suggest: Callable[[str, Path | None], Proposal] | None = None,
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
    run_suggest = suggest or _suggest

    def run() -> None:
        try:
            proposal = run_suggest(task, cwd)
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
        "source": proposal.source,
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
        cwd = project_dir(msg.get("cwd"))
        job_id = await asyncio.to_thread(start, _text(msg, "task"), cwd=cwd)
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
           "bottle_suggest_status", "bottle_suggestion_accept", "project_dir", "start",
           "status", "summary"]
