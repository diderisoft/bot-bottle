"""Launching a project from the console (PRD 0131).

- `launch` with `project` (`repo`, `path`, `branch`, `commit`): runs at that
  commit of a checkout this host reported, in a worktree
  (`project_worktrees`), from the checkout's place in it, as a CLI launch
  from there would. Refused before anything is made: a checkout it didn't
  report, a bad branch, or a commit it lacks even after a fetch.
- `launch_trust` (to the console): the launch needs project bottles or
  agents this host hasn't trusted. Every one is listed at once; the launch
  waits, its worktree kept, and the prompt is sent again on each connect
  until answered or 30 minutes pass.
- `launch_trust_answer` (`since`, `decision`, `hashes`): `trust` records
  each, as the CLI's y/N does but with the checkout's path, and the launch
  goes on; `decline` ends it. An answer to any other prompt is ignored.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...contract.auth import ROLE_CLI
from ...contract.hostctl import LaunchRequest
from ...lib import log
from ...manifest import trust
from ...manifest.trust import UntrustedBottleError
from . import checkouts, library_link, project_bottles, project_worktrees as wt
from .remote_requests import agent_allowed, launch_extras

TRUST_SECONDS = 30 * 60.0
MAX_ITEMS = 16
MAX_GRANTS, MAX_GRANT_CHARS, MAX_FILE_CHARS = 200, 300, 1024
_COMMIT = re.compile(r"[0-9a-f]{40}", re.ASCII)
TIMED_OUT = "not started: trust wasn't answered in 30 minutes"

Start = Callable[[str, LaunchRequest], Awaitable[None]]


class _Refused(Exception):
    """The launch is refused: a line for a person."""


def untrusted(request: LaunchRequest) -> list[UntrustedBottleError]:
    """Each project bottle and agent the launch needs that this host hasn't
    trusted, in the order found: each one found counts as trusted (in memory
    only) to find the next. Raises ManifestError for anything else wrong."""
    # pylint: disable-next=import-outside-toplevel
    from ...manifest import ManifestIndex
    index = ManifestIndex.resolve(request.cwd)
    tree = library_link.library_tree(request)
    if tree is not None:
        index = index.with_library(tree)
    found: list[UntrustedBottleError] = []
    while True:
        try:
            with trust.accepting(e.hash for e in found):
                index.load_for_agent(request.agent, request.bottles)
            return found
        except UntrustedBottleError as e:
            if len(found) >= MAX_ITEMS or any(f.hash == e.hash for f in found):
                raise
            found.append(e)


def _relative(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.resolve().relative_to(base.resolve()).as_posix()


def _shown(lines: Sequence[str]) -> list[str]:
    """`lines` within the prompt's bounds, never cut silently: a long line
    ends in "…", and past the limit the last line says how many more."""
    shown = [g if len(g) <= MAX_GRANT_CHARS else g[:MAX_GRANT_CHARS - 1] + "…" for g in lines]
    if len(shown) > MAX_GRANTS:
        more = len(shown) - MAX_GRANTS + 1
        shown = [*shown[:MAX_GRANTS - 1], f"… and {more} more (`bot-bottle trust` lists them)"]
    return shown


def ask(launch: wt.Launch, request: LaunchRequest,
        found: list[UntrustedBottleError]) -> wt.Launch:
    """`launch` waiting on trust for `found`: the prompt's items, and what
    trusting each records (the checkout's path of the file, not the
    worktree's, so the CLI and the next worktree see it)."""
    trusted = trust.load_trusted()
    items, records = [], []
    for error in found:
        here = _relative(error.path, launch.cwd())
        path = str(Path(launch.checkout) / here)
        previous = next((t for t in reversed(trusted) if t.path == path), None)
        items.append({
            "kind": error.kind, "name": error.bottle.removeprefix("./"), "hash": error.hash,
            "file": (launch.prefix + here)[:MAX_FILE_CHARS],
            "grants": _shown(error.grants),
            "previous": previous and {"trusted_at": previous.trusted_at,
                                      "grants": _shown(previous.grants)}})
        records.append({"kind": error.kind, "name": error.bottle, "hash": error.hash,
                        "path": path, "grants": list(error.grants)})
    pending = {"items": items, "records": records, "request": request.to_json()}
    return dataclasses.replace(launch, state=wt.TRUST, since=datetime.now(timezone.utc).isoformat(),
                               pending=json.dumps(pending))


def prompt(launch: wt.Launch) -> dict[str, Any]:
    """The `launch_trust` message for a launch waiting on trust."""
    return {"type": "launch_trust", "agent_id": launch.agent_id, "since": launch.since,
            "repo": launch.repo, "path": launch.checkout, "branch": launch.branch,
            "commit": launch.commit, "items": json.loads(launch.pending)["items"]}


def record(launch: wt.Launch) -> None:
    """Trust each pending item, as the CLI does after its y/N."""
    for item in json.loads(launch.pending)["records"]:
        trust.trust(UntrustedBottleError(
            item["name"], Path(item["path"]), digest=item["hash"], granted=item["grants"],
            previous=None, kind=item["kind"]))


def declined(launch: wt.Launch) -> str:
    """Why a declined launch didn't start, naming each as `bot-bottle trust`
    takes it (`./dev` for a project bottle)."""
    names = [r["name"] for r in json.loads(launch.pending)["records"]]
    return (f"not started: {', '.join(names)} {'stays' if len(names) == 1 else 'stay'} "
            f"untrusted on this host (`bot-bottle trust {names[0]}` in "
            f"{launch.checkout} to review it)")[:500]


def _expired(launch: wt.Launch, now: float) -> bool:
    try:
        return now - datetime.fromisoformat(launch.since).timestamp() >= TRUST_SECONDS
    except ValueError:
        return True


def prepare(agent_id: str, msg: dict[str, Any], running: dict[str, str],
            launches: wt.ProjectLaunchStore,
            kept: checkouts.CheckoutStore) -> tuple[wt.Launch, LaunchRequest]:
    """Check a project launch and make its worktree: the launch, starting,
    and its request. Raises _Refused, saying why, before anything is made."""
    definition = msg.get("definition")
    definition = definition if isinstance(definition, dict) else {}
    if "content" in definition:
        raise _Refused("this host launches only its own agents, by name; "
                       "console-defined agent content isn't accepted")
    name, extras = definition.get("name"), launch_extras(msg, project=True)
    if isinstance(extras, str):
        raise _Refused(extras)
    if not agent_allowed(name, lambda: (), "", project=True):
        raise _Refused(f"{name!r} isn't an agent name")
    if not wt.AGENT_ID.fullmatch(agent_id):
        raise _Refused("a project launch needs the console's agent id")
    project = msg.get("project")
    fields = [project.get(k) for k in ("repo", "path", "branch", "commit")] \
        if isinstance(project, dict) else []
    if len(fields) != 4 or not all(isinstance(f, str) for f in fields):
        raise _Refused("project needs a repo, a path, a branch and a commit")
    repo, path, branch, commit = (str(f) for f in fields)
    if not _COMMIT.fullmatch(commit):
        raise _Refused("project.commit must be a 40-character commit sha")
    checkout = kept.get(path) if path else None
    if (checkout is None or checkout.repo != repo or not checkout.reportable()
            or not Path(path).is_dir()):
        raise _Refused(f"this host has no checkout of {repo} at {path}")
    if branch and not project_bottles.valid_branch(Path(path), branch):
        raise _Refused(f"{branch!r} isn't a branch name")
    top = checkouts.git(Path(path), "rev-parse", "--show-toplevel").stdout.strip()
    if not top:
        raise _Refused(f"{path} isn't in a git work tree anymore")
    if not wt.has_commit(Path(top), commit):
        remote = checkouts.remote_name(Path(top))
        if not (remote and not remote.startswith("-") and project_bottles.fetch(Path(top), remote)
                and wt.has_commit(Path(top), commit)):
            raise _Refused(f"commit {commit[:12]} isn't in {path}; look at the project again")
    if agent_id in running:
        raise _Refused(f"{agent_id} is already running")
    rel = Path(path).resolve().relative_to(Path(top).resolve()).as_posix()
    launch = wt.Launch(agent_id=agent_id, checkout=path, repo=repo, branch=branch,
                       commit=commit, worktree=str(wt.worktree_for(agent_id)),
                       prefix="" if rel == "." else rel + "/", state=wt.STARTING,
                       created_at=time.time())
    launches.put(launch)  # first, so a restart mid-make sweeps it
    try:
        wt.make(launch)
    except Exception as e:  # pylint: disable=broad-exception-caught
        wt.remove(launch.checkout, Path(launch.worktree))
        launches.delete(agent_id)
        raise _Refused(str(e) if isinstance(e, wt.WorktreeError)
                       else f"couldn't make a worktree of {path}: {e}") from e
    # The console click is the launch confirmation.
    request = LaunchRequest(agent=str(name), assume_yes=True, headless=False,
                            agent_id=agent_id, cwd=str(launch.cwd()), **extras)
    return launch, request


class ProjectLaunches:
    """Project launches on one console link: `start` launches a request as
    any console launch (`ConsoleLink._start`); `running` is the link's
    running agents."""

    REQUESTS = frozenset({"launch_trust_answer"})

    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[None]], start: Start,
                 running: Callable[[], dict[str, str]], *,
                 store: Callable[[], wt.ProjectLaunchStore] = wt.store,
                 kept: Callable[[], checkouts.CheckoutStore] = checkouts.store) -> None:
        self._send = send
        self._start = start
        self._running = running
        self._store = store
        self._kept = kept

    async def _errored(self, agent_id: str, message: str) -> None:
        await self._send({"type": "agent_errored", "agent_id": agent_id,
                          "message": " ".join(message.split())[:500]})

    async def _end(self, launch: wt.Launch, message: str) -> None:
        """The launch failed: its worktree and row go, and the console hears why."""
        await asyncio.to_thread(wt.remove, launch.checkout, Path(launch.worktree))
        await asyncio.to_thread(self._store().delete, launch.agent_id)
        await self._errored(launch.agent_id, message)

    async def launch(self, agent_id: str, msg: dict[str, Any]) -> None:
        """A console `launch` naming a `project`, in its agent's turn."""
        try:
            launch, request = await asyncio.to_thread(
                prepare, agent_id, msg, dict(self._running()), self._store(), self._kept())
        except _Refused as e:
            await self._errored(agent_id, str(e))
            return
        except Exception as e:  # pylint: disable=broad-exception-caught  # never left waiting
            await self._errored(agent_id, f"couldn't read the project: {e}")
            return
        await self._go(launch, request)

    async def _go(self, launch: wt.Launch, request: LaunchRequest) -> None:
        """Resolve in the worktree: launch, or ask for trust first."""
        try:
            found = await asyncio.to_thread(untrusted, request)
        except (Exception, SystemExit) as e:  # pylint: disable=broad-exception-caught
            await self._end(launch, f"launch failed: {getattr(e, 'message', '') or e}")
            return
        if found:
            try:
                asking = await asyncio.to_thread(ask, launch, request, found)
                await asyncio.to_thread(self._store().put, asking)
            except Exception as e:  # pylint: disable=broad-exception-caught
                await self._end(launch, f"couldn't ask for trust: {e}")
                return
            await self._send(prompt(asking))
            return
        await self._start(launch.agent_id, request)
        bottle = self._running().get(launch.agent_id)
        if bottle:
            await asyncio.to_thread(self._store().put, dataclasses.replace(
                launch, state=wt.RUNNING, bottle=bottle, since="", pending=""))
        else:  # it failed, and said so
            await asyncio.to_thread(wt.remove, launch.checkout, Path(launch.worktree))
            await asyncio.to_thread(self._store().delete, launch.agent_id)

    async def answer(self, msg: dict[str, Any]) -> None:
        """`launch_trust_answer`, in its agent's turn: acted on only for the
        prompt still pending, with exactly its items for `trust`."""
        agent_id, since = str(msg.get("agent_id", "")), msg.get("since")
        decision, hashes = msg.get("decision"), msg.get("hashes")
        launch = await asyncio.to_thread(self._store().get, agent_id)
        if launch is None or launch.state != wt.TRUST or since != launch.since:
            log.info(f"remote: ignoring a stale trust answer for {agent_id}")
            return
        if decision == "trust":
            wanted = {r["hash"] for r in json.loads(launch.pending)["records"]}
            if (not isinstance(hashes, list) or not all(isinstance(h, str) for h in hashes)
                    or set(hashes) != wanted):
                log.info(f"remote: ignoring a trust answer for {agent_id} that isn't "
                         "for what's pending")
                return
        elif decision != "decline":
            return
        claimed = await asyncio.to_thread(self._store().claim, agent_id, launch.since,
                                          wt.STARTING)
        if claimed is None:
            return  # timed out meanwhile
        if decision == "decline":
            await self._end(claimed, declined(claimed))
            return
        try:
            await asyncio.to_thread(record, claimed)
        except Exception as e:  # pylint: disable=broad-exception-caught
            await self._end(claimed, f"couldn't record the trust: {e}")
            return
        request = LaunchRequest.from_json(json.loads(claimed.pending)["request"], ROLE_CLI)
        await self._go(dataclasses.replace(claimed, since="", pending=""), request)

    async def ended(self, agent_id: str) -> None:
        """`agent_id` was torn down (`agent_stopped`, or it exited): its
        worktree goes."""
        try:
            launch = await asyncio.to_thread(self._store().get, agent_id)
            if launch is not None and launch.state == wt.RUNNING:
                await asyncio.to_thread(wt.ended, launch, self._store())
        except Exception as e:  # pylint: disable=broad-exception-caught  # swept on start
            log.warn(f"projects: couldn't remove {agent_id}'s worktree: {e}")

    async def connected(self) -> None:
        """A (re)connected console hears each launch a restart ended, and
        each trust prompt still pending."""
        for launch in await asyncio.to_thread(self._store().all):
            if launch.state == wt.ERRORED:
                await asyncio.to_thread(self._store().delete, launch.agent_id)
                await self._errored(launch.agent_id, launch.message)
            elif launch.state == wt.TRUST and not await self._timed_out(launch):
                await self._send(prompt(launch))

    async def tick(self) -> None:
        """On the link's watch loop: end each prompt unanswered for 30 minutes."""
        for launch in await asyncio.to_thread(self._store().all):
            if launch.state == wt.TRUST:
                await self._timed_out(launch)

    async def _timed_out(self, launch: wt.Launch) -> bool:
        if not _expired(launch, time.time()):
            return False
        claimed = await asyncio.to_thread(self._store().claim, launch.agent_id, launch.since,
                                          wt.STARTING)
        if claimed is not None:
            await self._end(claimed, TIMED_OUT)
        return True


__all__ = ["MAX_ITEMS", "ProjectLaunches", "TIMED_OUT", "TRUST_SECONDS", "ask", "declined",
           "prepare", "prompt", "record", "untrusted"]
