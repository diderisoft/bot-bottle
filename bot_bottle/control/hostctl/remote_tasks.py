"""Asyncio helpers for the console link (`remote.py`): requests that wait on
a bottle taken in turn per agent, and a connection raced against shutdown."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from ...lib import log


class AgentTurns:
    """Runs each agent's slow requests (launch, resume, stop) one after
    another, beside the caller: a teardown that wedged held every request
    behind it, for any agent."""

    def __init__(self) -> None:
        # agent id -> its latest request, still running
        self.pending: dict[str, asyncio.Task[None]] = {}

    def submit(self, agent_id: str, work: Callable[[], Awaitable[None]]) -> None:
        """Start `work` once `agent_id`'s previous request is done."""
        before = self.pending.get(agent_id)

        async def in_turn() -> None:
            if before is not None:
                await asyncio.wait({before})
            await work()

        task = asyncio.create_task(in_turn())
        self.pending[agent_id] = task
        task.add_done_callback(lambda t: self._done(agent_id, t))

    def _done(self, agent_id: str, task: "asyncio.Task[None]") -> None:
        if self.pending.get(agent_id) is task:
            del self.pending[agent_id]
        if not task.cancelled() and task.exception() is not None:
            log.warn(f"remote: {agent_id}: {task.exception()}")


class Stopped(Exception):
    """`stop` was set while the link was dialing or serving."""


async def unless_stopped(coro: Awaitable[Any], stopped: "asyncio.Future[Any]") -> Any:
    """`coro`'s result, or `Stopped` (with `coro` cancelled) once `stopped`
    resolves. `stopped` is shared and never cancelled here."""
    task = asyncio.ensure_future(coro)
    await asyncio.wait({task, stopped}, return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        return task.result()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    raise Stopped


__all__ = ["AgentTurns", "Stopped", "unless_stopped"]
