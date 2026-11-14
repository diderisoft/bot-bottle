"""Projects on the console link (PRD 0131).

- `projects` (`checkouts`, from the host): every checkout this host keeps
  (`checkouts.py`), the full list each time. Sent on each connect, and
  whenever it changes: an add or remove, a launch, or a refresh (each
  minute, beside the link: it runs git in every checkout) that dropped a
  checkout or saw a new repo or branch.
- `project_bottles` (`repo`, `path`, `branch`, `request_id`): a reported
  checkout's bottles and agents at a branch (`project_bottles.py`), replied
  to as `project_bottles`. Answered beside the link, since it fetches, a
  couple at a time.
- A `launch` naming a `project`, `launch_trust` and `launch_trust_answer`:
  `project_launch.py`.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ...lib import log
from ...contract.hostctl import LaunchRequest
from . import checkouts, project_bottles
from .checkouts import CheckoutStore
from .project_launch import ProjectLaunches

REFRESH_SECONDS = 60.0
MAX_ANSWERING = 2  # `project_bottles` read at once; more wait their turn


class ProjectLink:
    """The projects' messages on one console link."""

    REQUESTS = frozenset({"project_bottles"}) | ProjectLaunches.REQUESTS

    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[None]], *,
                 store: Callable[[], CheckoutStore] = checkouts.store,
                 start: Callable[[str, LaunchRequest], Awaitable[None]] | None = None,
                 running: Callable[[], dict[str, str]] = dict) -> None:
        self._send = send
        self.launches = ProjectLaunches(send, start or _no_start, running, kept=store)
        self._make_store = store
        self._store: CheckoutStore | None = None
        self._told: dict[str, object] | None = None  # the report the console has
        self._refreshed = 0.0
        self._refreshing: asyncio.Task[None] | None = None
        self._asked: set[asyncio.Task[None]] = set()
        self._answering = asyncio.Semaphore(MAX_ANSWERING)

    def store(self) -> CheckoutStore:
        if self._store is None:
            self._store = self._make_store()
        return self._store

    async def handle(self, msg: dict[str, Any]) -> None:
        """A `project_bottles` request, answered once read (it may fetch), or
        a `launch_trust_answer` (in its agent's turn)."""
        if msg.get("type") in ProjectLaunches.REQUESTS:
            await self.launches.answer(msg)
            return
        task = asyncio.create_task(self._answer(msg))
        self._asked.add(task)
        task.add_done_callback(self._asked.discard)

    async def _answer(self, msg: dict[str, Any]) -> None:
        async with self._answering:
            try:
                reply = await asyncio.to_thread(project_bottles.answer, msg, self.store())
            except (OSError, sqlite3.Error) as e:
                reply = {"type": "project_bottles", "request_id": msg.get("request_id"),
                         "error": f"couldn't read this host's checkouts: {e}"[:300]}
        await self._send(reply)

    async def connected(self) -> None:
        """A (re)connected console hears every checkout at once (and again
        once they're re-read if that changed anything), then the project
        launches' news."""
        self._told = None
        self._refreshed = 0.0
        await self.tick()
        try:
            await self.launches.connected()
        except (OSError, sqlite3.Error) as e:
            log.warn(f"projects: couldn't read this host's project launches: {e}")

    async def tick(self) -> None:
        """On the link's watch loop: start a re-read of every checkout each
        minute (beside the loop: it's a few gits per checkout), and send the
        report when it isn't what the console has."""
        idle = self._refreshing is None or self._refreshing.done()
        if idle and time.monotonic() - self._refreshed >= REFRESH_SECONDS:
            self._refreshed = time.monotonic()
            self._refreshing = asyncio.create_task(self._refresh())
        try:
            message = await asyncio.to_thread(checkouts.report, self.store())
        except (OSError, sqlite3.Error) as e:
            log.debug(f"projects: couldn't read this host's checkouts: {e}")
            return
        if message != self._told:
            await self._send(message)
            self._told = message
        try:
            await self.launches.tick()
        except (OSError, sqlite3.Error) as e:
            log.debug(f"projects: couldn't read this host's project launches: {e}")

    async def _refresh(self) -> None:
        try:
            await asyncio.to_thread(checkouts.refresh, self.store())
        except (OSError, sqlite3.Error) as e:
            log.debug(f"projects: couldn't re-read this host's checkouts: {e}")

    def close(self) -> None:
        for task in [*self._asked, *filter(None, [self._refreshing])]:
            task.cancel()


async def _no_start(agent_id: str, request: LaunchRequest) -> None:
    del request
    raise RuntimeError(f"no launcher for {agent_id}")


__all__ = ["MAX_ANSWERING", "ProjectLink", "REFRESH_SECONDS"]
