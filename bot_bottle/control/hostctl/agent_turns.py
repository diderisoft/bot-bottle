"""Each console agent's slow requests (launch, resume, stop), one at a time
per agent, beside the console link (`remote.py`)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from ...lib import log


class AgentTurns:
    """Runs each agent's slow requests (launch, resume, stop) in turn, beside
    the caller, so a wedged teardown holds up only its own agent."""

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


__all__ = ["AgentTurns"]
