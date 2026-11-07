"""The console link's canary events: telling the console when a running
agent's canary fires, and forwarding the console's resolution back.

- `canary_fired` (from the host): `agent_id`, `event_id`, `bottle_slug`,
  `reason`, `request_path`, `fired_at`; once per event per connection.
- `resolve_canary` (`event_id`, `bottle_slug`, `action`): passed to the
  orchestrator; the event is sent again if it's still unresolved.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ...lib import log


class CanaryLink:
    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[None]],
                 orchestrator: Callable[[], Any],
                 running: Callable[[], dict[str, str]]) -> None:
        self._send = send
        self._orchestrator = orchestrator
        self._running = running  # agent id -> bottle slug
        # Event ids already sent in this connection; re-sent on reconnect.
        self._sent: set[str] = set()

    async def push(self) -> None:
        """Send `canary_fired` for unresolved canary events whose bottle is a
        running agent; only new ones since the last push. Called on the tick
        loop so the console learns about canaries within seconds of them
        firing."""
        running = self._running()
        if not running:
            return
        bottle_to_agent = {v: k for k, v in running.items()}
        try:
            events = await asyncio.to_thread(self._orchestrator().canary_events)
        except Exception as e:  # pylint: disable=broad-exception-caught  # best effort
            log.debug(f"remote: can't fetch canary events: {e}")
            return
        for ev in events:
            if not isinstance(ev, dict):
                continue
            event_id = str(ev.get("id") or "")
            bottle_slug = str(ev.get("bottle_slug") or "")
            agent_id = bottle_to_agent.get(bottle_slug)
            if not event_id or event_id in self._sent or agent_id is None:
                continue
            self._sent.add(event_id)
            await self._send({
                "type": "canary_fired",
                "agent_id": agent_id,
                "event_id": event_id,
                "bottle_slug": bottle_slug,
                "reason": str(ev.get("reason") or ""),
                "request_path": str(ev.get("request_path") or ""),
                "fired_at": str(ev.get("arrival_timestamp") or ""),
            })

    async def resolve(self, msg: dict[str, Any]) -> None:
        """Forward a canary resolution from the console to the orchestrator."""
        event_id = str(msg.get("event_id", ""))
        bottle_slug = str(msg.get("bottle_slug", ""))
        action = str(msg.get("action", ""))
        if not event_id or not bottle_slug or not action:
            return
        try:
            await asyncio.to_thread(self._orchestrator().canary_resolve, event_id,
                                    bottle_slug=bottle_slug, action=action)
            self._sent.discard(event_id)
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.warn(f"remote: couldn't resolve canary event {event_id!r}: {e}")
