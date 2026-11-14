"""Projects on the console link (PRD 0131).

- `projects` (`checkouts`, from the host): every checkout this host keeps
  (`checkouts.py`), the full list each time. Sent on each connect, and
  whenever it changes: an add or remove, a launch, or a refresh (each
  minute) that dropped a checkout or saw a new repo or branch.
- `project_bottles` (`repo`, `path`, `branch`, `request_id`): a reported
  checkout's bottles and agents at a branch (`project_bottles.py`), replied
  to as `project_bottles`. Answered beside the link, since it fetches.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ...lib import log
from . import checkouts, project_bottles
from .checkouts import CheckoutStore

REFRESH_SECONDS = 60.0


class ProjectLink:
    """The projects' messages on one console link."""

    REQUESTS = frozenset({"project_bottles"})

    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[None]], *,
                 store: Callable[[], CheckoutStore] = checkouts.store) -> None:
        self._send = send
        self._make_store = store
        self._store: CheckoutStore | None = None
        self._told: dict[str, object] | None = None  # the report the console has
        self._refreshed = 0.0
        self._asked: set[asyncio.Task[None]] = set()

    def store(self) -> CheckoutStore:
        if self._store is None:
            self._store = self._make_store()
        return self._store

    async def handle(self, msg: dict[str, Any]) -> None:
        """A `project_bottles` request, answered once read (it may fetch)."""
        task = asyncio.create_task(self._answer(msg))
        self._asked.add(task)
        task.add_done_callback(self._asked.discard)

    async def _answer(self, msg: dict[str, Any]) -> None:
        try:
            reply = await asyncio.to_thread(project_bottles.answer, msg, self.store())
        except (OSError, sqlite3.Error) as e:
            reply = {"type": "project_bottles", "request_id": msg.get("request_id"),
                     "error": f"couldn't read this host's checkouts: {e}"[:300]}
        await self._send(reply)

    async def connected(self) -> None:
        """A (re)connected console hears every checkout."""
        self._told = None
        await self.tick(refresh=True)

    async def tick(self, *, refresh: bool = False) -> None:
        """On the link's watch loop: refresh each minute, and send the
        report when it isn't what the console has."""
        try:
            if refresh or time.monotonic() - self._refreshed >= REFRESH_SECONDS:
                self._refreshed = time.monotonic()
                await asyncio.to_thread(checkouts.refresh, self.store())
            message = await asyncio.to_thread(checkouts.report, self.store())
        except (OSError, sqlite3.Error) as e:
            log.debug(f"projects: couldn't read this host's checkouts: {e}")
            return
        if message != self._told:
            await self._send(message)
            self._told = message

    def close(self) -> None:
        for task in list(self._asked):
            task.cancel()


__all__ = ["ProjectLink", "REFRESH_SECONDS"]
