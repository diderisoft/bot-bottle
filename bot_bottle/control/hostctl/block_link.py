"""The console link's block alerts: what a running agent's bottle just
blocked, for the shield on its page. Ephemeral, unlike an approval, which
waits for an answer: the console keeps them a while, in memory, and the audit
log keeps the record.

- `blocked` (from the host): `agent_id`, `host`, `reason` (a code in
  `contract.blocks.REASONS`), `count` (blocks of that host and reason merged
  since the last send), `at` (the latest, ISO 8601), and, for a secret or a
  rejected push, `detail` (the gateway's own words, which name the detector,
  never the value). Never a path or a body: either can hold the secret.

The host controller's audit listener fills one process-wide feed
(`FEED.on_event`). The host runs one console link at a time: a reconnect goes
on from the last block sent (sending one again would count it twice), and the
first connection after a restart sends the last `RECENT_SECONDS`.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ...contract.audit import AuditEvent
from ...contract.blocks import PUSH, SECRET
from .blocks import blocked

FEED_MAX = 500
DETAIL_MAX = 160
HOST_MAX = 253
RECENT_SECONDS = 300.0  # a connection that opens sends blocks this recent
SEND_MAX = 20  # distinct (agent, host, reason) a push sends; the rest wait
DETAILED = frozenset({SECRET, PUSH})


@dataclass(frozen=True)
class Block:
    seq: int
    session: str
    host: str
    code: str
    detail: str
    at: str  # ISO 8601, UTC
    when: float  # monotonic


class BlockFeed:
    """The latest blocks of every bottle, in arrival order; thread-safe (the
    audit listener adds, each link's loop reads)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._blocks: deque[Block] = deque(maxlen=FEED_MAX)
        self._seq = 0
        self.sent: int | None = None  # the last block a link sent, once one has
        self._lock = threading.Lock()

    def on_event(self, event: AuditEvent, producer: str) -> None:
        """An audit event the host controller heard (`HostAudit.listen`)."""
        found = blocked(event, producer)
        if found is None:
            return
        session, host, code = found
        detail = ""
        if code in DETAILED:
            # The gateway's words are its `reason`; git-gate's, its `detail`.
            payload = event.payload or {}
            words = payload.get("reason") or payload.get("detail")
            detail = words[:DETAIL_MAX] if isinstance(words, str) else ""
        self.add(session, host, code, detail)

    def add(self, session: str, host: str, code: str, detail: str = "") -> None:
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            self._seq += 1
            self._blocks.append(Block(self._seq, session, host[:HOST_MAX], code, detail, at,
                                      self._clock()))

    def since(self, seq: int) -> list[Block]:
        """Blocks after `seq`, oldest first."""
        with self._lock:
            return [b for b in self._blocks if b.seq > seq]

    def start(self, recent: float = RECENT_SECONDS) -> int:
        """Where a new connection starts: after the last block sent, or, the
        first time, just before the blocks of the last `recent` seconds."""
        if self.sent is not None:
            return self.sent
        cutoff = self._clock() - recent
        with self._lock:
            newer = [b.seq for b in self._blocks if b.when >= cutoff]
            return newer[0] - 1 if newer else self._seq


FEED = BlockFeed()


class BlockLink:
    """Sends one connection the blocks of its running agents, merged by host
    and reason per push."""

    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[None]],
                 running: Callable[[], dict[str, str]], feed: BlockFeed = FEED) -> None:
        self._send = send
        self._running = running  # console agent id -> bottle id
        self._feed = feed
        self._seq = feed.start()

    async def push(self) -> None:
        new = self._feed.since(self._seq)
        if not new:
            return
        agent_of = {bottle: agent for agent, bottle in self._running().items()}
        merged: dict[tuple[str, str, str], dict[str, Any]] = {}
        for block in new:
            agent = agent_of.get(block.session)
            key = (agent or "", block.host, block.code)
            if agent is None:
                continue
            if key not in merged and len(merged) >= SEND_MAX:
                break  # the rest go next push
            self._seq = block.seq
            msg = merged.setdefault(key, {"type": "blocked", "agent_id": agent,
                                          "host": block.host, "reason": block.code,
                                          "count": 0})
            msg["count"] += 1
            msg["at"] = block.at
            if block.detail:
                msg["detail"] = block.detail
        else:
            self._seq = new[-1].seq
        for msg in merged.values():
            await self._send(msg)
        self._feed.sent = self._seq


__all__ = ["FEED", "Block", "BlockFeed", "BlockLink"]
