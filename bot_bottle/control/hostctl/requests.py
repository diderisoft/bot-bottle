"""Show agents' pending requests in their session status bars. A thread polls
the orchestrator and sets each held session's `@bb_request` tmux option to a
one-line summary, drawn outside the agent's pane where the agent can't fake or
hide it.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable

from ...approvals import Request
from ...lib import log
from .sessions import tmux

POLL_SECONDS = 1.0
BACKOFF_SECONDS = 5.0


def summaries(pending: list[dict[str, object]], sessions: list[str]) -> dict[str, str]:
    """Each session's status line: its oldest request, and how many more."""
    by_session: dict[str, list[Request]] = {s: [] for s in sessions}
    for raw in pending:
        try:
            request = Request.from_dict(dict(raw))
        except (KeyError, TypeError, ValueError):
            continue
        if request.session in by_session:
            by_session[request.session].append(request)
    out: dict[str, str] = {}
    for session, requests in by_session.items():
        if not requests:
            out[session] = ""
            continue
        more = f" (+{len(requests) - 1} more)" if len(requests) > 1 else ""
        out[session] = requests[0].summary() + more
    return out


def _set_status(session: str, text: str) -> None:
    subprocess.run(tmux("set-option", "-t", f"={session}:", "@bb_request", text),
                   capture_output=True, check=False)


class RequestNotifier:
    """Polls while there are held sessions; writes only what changed."""

    def __init__(self, held: Callable[[], list[str]],
                 fetch: Callable[[], list[dict[str, object]]],
                 set_status: Callable[[str, str], None] = _set_status) -> None:
        self._held = held
        self._fetch = fetch
        self._set_status = set_status
        self._shown: dict[str, str] = {}
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="hostctl-requests").start()

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> bool:
        """One poll. False when the orchestrator couldn't be asked."""
        sessions = self._held()
        if not sessions:
            self._shown.clear()
            return True
        try:
            pending = self._fetch()
        except Exception as e:  # pylint: disable=broad-exception-caught  # retried with backoff
            log.debug(f"requests: couldn't list pending proposals: {e}")
            return False
        for session, text in summaries(pending, sessions).items():
            if self._shown.get(session) != text:
                self._set_status(session, text)
                self._shown[session] = text
        return True

    def _loop(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            if not self.tick():
                self._stop.wait(BACKOFF_SECONDS)


def orchestrator_pending() -> list[dict[str, object]]:
    """Pending proposals from the environment's orchestrator, each tagged
    with its URL."""
    # pylint: disable=import-outside-toplevel  # only once a bottle is held
    from ..client import pending_proposals
    return pending_proposals()


__all__ = ["RequestNotifier", "orchestrator_pending", "summaries"]
