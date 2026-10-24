"""Show what each bottle blocked in its session status bar (PRD 0099).

The gateway's block events reach the host controller as audit events;
`BlockNotifier` hears them through `HostAudit.listen` and keeps, per session,
a count per reason and a short-lived notice for the latest block. A thread
writes them to three tmux session options the status bar draws, outside the
agent's pane:

  @bb_block_now   `api.example.com · not on allowlist (+2)` while a notice shows
  @bb_block_host  the same notice without the reason, for narrower clients
  @bb_blocks      `A12 S3 P1`, the session's counts by reason letter
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ...contract.audit import GATEWAY_PRODUCERS, AuditEvent
from ...contract.blocks import REASON_CODE_KEY, REASONS, reason_code
from .sessions import tmux

NOTICE_SECONDS = 5.0  # a notice shows this long after the latest block
NOTICE_MAX_SECONDS = 10.0  # however many blocks keep arriving
QUIET_SECONDS = 5.0  # then the counts show at least this long
TICK_SECONDS = 0.25
HOST_MAX = 40


@dataclass
class _Session:
    counts: dict[str, int] = field(default_factory=dict)
    host: str = ""
    code: str = ""
    extra: int = 0  # other blocks in the current notice's burst
    start: float = float("-inf")
    until: float = float("-inf")


def _escape(text: str) -> str:
    """`text` safe inside a tmux format: `#` doubled."""
    return text.replace("#", "##")


def _short(host: str) -> str:
    return host if len(host) <= HOST_MAX else host[:HOST_MAX - 1] + "…"


def _blocked_host(event: AuditEvent) -> str:
    payload = event.payload or {}
    for key in ("host", "repository"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return event.resource or "?"


def render(state: _Session, now: float) -> dict[str, str]:
    """The option values for one session at `now`."""
    letters = list(REASONS)
    counts = sorted(state.counts.items(), key=lambda kv: (-kv[1], letters.index(kv[0])))
    out = {"@bb_blocks": " ".join(f"{REASONS[c][0]}{n}" for c, n in counts),
           "@bb_block_now": "", "@bb_block_host": ""}
    if now < state.until:
        more = f" (+{state.extra})" if state.extra else ""
        host = _escape(_short(state.host))
        out["@bb_block_host"] = f"{host}{more}"
        out["@bb_block_now"] = f"{host} · {REASONS[state.code][1]}{more}"
    return out


def _set_option(session: str, name: str, value: str) -> None:
    subprocess.run(tmux("set-option", "-t", f"={session}:", name, value),
                   capture_output=True, check=False)


class BlockNotifier:
    """Counts the blocks of the sessions `held` returns; writes only what
    changed."""

    def __init__(self, held: Callable[[], list[str]], *,
                 set_option: Callable[[str, str, str], None] = _set_option,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._held = held
        self._set_option = set_option
        self._clock = clock
        self._sessions: dict[str, _Session] = {}
        self._shown: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def on_event(self, event: AuditEvent, producer: str) -> None:
        """An audit event the host controller heard (`HostAudit.listen`)."""
        if producer not in GATEWAY_PRODUCERS or event.outcome != "blocked":
            return
        payload = event.payload or {}
        if REASON_CODE_KEY not in payload and producer == "supervise":
            return  # a supervise decision; the gateway reports the block itself
        session = event.bottle or ""
        if session not in self._held():
            return
        self.block(session, _blocked_host(event), reason_code(payload.get(REASON_CODE_KEY)))

    def block(self, session: str, host: str, code: str) -> None:
        now = self._clock()
        with self._lock:
            state = self._sessions.setdefault(session, _Session())
            state.counts[code] = state.counts.get(code, 0) + 1
            if now < state.until:  # in a burst: the newest replaces the text
                state.host, state.code, state.extra = host, code, state.extra + 1
                state.until = min(now + NOTICE_SECONDS, state.start + NOTICE_MAX_SECONDS)
            elif now >= state.until + QUIET_SECONDS:  # else counts only, for now
                state.host, state.code, state.extra = host, code, 0
                state.start, state.until = now, now + NOTICE_SECONDS

    def tick(self) -> None:
        """Write each session's changed options; forget ended sessions."""
        held = set(self._held())
        now = self._clock()
        with self._lock:
            for gone in [s for s in self._sessions if s not in held]:
                del self._sessions[gone]
                self._shown.pop(gone, None)
            rendered = {s: render(state, now) for s, state in self._sessions.items()}
        for session, values in rendered.items():
            shown = self._shown.setdefault(session, {})
            for name, value in values.items():
                if shown.get(name, "") != value:
                    self._set_option(session, name, value)
                    shown[name] = value

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="hostctl-blocks").start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            self.tick()


__all__ = ["BlockNotifier", "render"]
