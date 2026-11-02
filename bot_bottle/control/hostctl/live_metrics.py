"""The console's live resource gauge: CPU % and memory in use, every 3 seconds,
for only the bottles someone is looking at.

`metrics_watch` (`bottles`: ids) replaces the link's watched set; an empty
list stops it. While the set is non-empty the link samples the watched
bottles it has running in one pass every `LIVE_METRICS_INTERVAL_SECONDS`, for
every backend, and sends `bottle_metrics` (`at`, `bottles`: `id`, `cpu_pct`,
`mem_used`, `memory`). The interval leaves room for a container backend's
`stats` call (1-2 s each); a pass still running when the next is due skips
it rather than queueing. The set belongs to one connection: a
reconnecting console sends it again. The minute `host_stats` keeps its own CPU
baseline (`bottle_metrics.collect`'s `sample`)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from ...lib import log

LIVE_METRICS_INTERVAL_SECONDS = 3.0
MAX_WATCHED = 32
_MAX_ID = 200

Sampler = Callable[[str], "dict[str, object] | None"]


def watched(raw: object) -> list[str]:
    """The bottle ids in a `metrics_watch`, deduplicated and capped."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for b in raw:
        if isinstance(b, str) and 0 < len(b) <= _MAX_ID and b not in out:
            out.append(b)
        if len(out) >= MAX_WATCHED:
            break
    return out


class MetricsWatch:
    """One connection's watched bottles and its sampling loop (`run`)."""

    def __init__(self, send: Callable[[dict[str, Any]], Awaitable[None]],
                 running: Callable[[], set[str]], sample: Sampler | None) -> None:
        self._send = send
        self._running = running
        self._sample = sample  # BottleService.live_metrics; None: this host can't
        self.bottles: list[str] = []
        self._busy = False

    def watch(self, raw: object) -> None:
        self.bottles = watched(raw)

    def _collect(self, ids: list[str]) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for bottle in ids:
            try:
                entry = self._sample(bottle) if self._sample is not None else None
            except Exception as e:  # pylint: disable=broad-exception-caught  # a gauge, best effort
                log.debug(f"remote: sampling {bottle} failed: {e}")
                entry = None
            if entry is not None:
                out.append(entry)
        return out

    async def tick(self) -> bool:
        """Sample and send once; False when there was nothing to do or the
        last sample is still running."""
        if self._busy or self._sample is None:
            return False
        running = self._running()
        ids = [b for b in self.bottles if b in running]
        if not ids:
            return False
        self._busy = True
        try:
            entries = await asyncio.to_thread(self._collect, ids)
        finally:
            self._busy = False
        if not entries:
            return False
        await self._send({"type": "bottle_metrics",
                          "at": datetime.now(timezone.utc).isoformat(), "bottles": entries})
        return True

    async def run(self) -> None:
        """Tick every `LIVE_METRICS_INTERVAL_SECONDS` while the connection
        lasts. A slow pass runs on in its thread; ticks meanwhile are skipped."""
        pending: asyncio.Future[bool] | None = None
        try:
            while True:
                await asyncio.sleep(LIVE_METRICS_INTERVAL_SECONDS)
                if pending is not None:
                    if not pending.done():
                        continue
                    if not pending.cancelled() and (e := pending.exception()) is not None:
                        log.debug(f"remote: sending bottle metrics failed: {e}")
                    pending = None
                if self.bottles:
                    pending = asyncio.ensure_future(self.tick())
        finally:
            if pending is not None:
                pending.cancel()


__all__ = ["LIVE_METRICS_INTERVAL_SECONDS", "MAX_WATCHED", "MetricsWatch", "watched"]
