"""bot-bottle resolve-canary: acknowledge or terminate a bottle after a canary
fires.

  bot-bottle resolve-canary [bottle]

A canary hit means the agent tried to send its own secret token over egress —
a strong signal of an exfiltration attempt. When one fires, the bottle keeps
running but the event is recorded here until an operator resolves it.

Each unresolved event is shown with:
  <event-id>  <bottle>  <when>  <reason>  <path>

Keys:

  a  acknowledge  — reviewed and cleared; the bottle may continue
  t  terminate    — shut the bottle down and record the reason

"Terminate" deregisters the bottle from the orchestrator immediately (cutting
off its identity) and starts a host-controller teardown if the host controller
is reachable.

Filtering by bottle: pass the bottle's session name or slug to show only that
bottle's events.
"""

from __future__ import annotations

import sys
import termios
import tty
from collections.abc import Callable, Mapping, Sequence

from ...control.client import (
    OrchestratorClient,
    OrchestratorClientError,
    env_orchestrator_url,
)
from ...contract.supervise import CANARY_ACTION_ACKNOWLEDGE, CANARY_ACTION_TERMINATE

_DIM, _BOLD, _RED, _RESET = "\x1b[2m", "\x1b[1m", "\x1b[31m", "\x1b[0m"


def read_key() -> str:
    """One keypress, unbuffered (falls back to line input without a terminal)."""
    if not sys.stdin.isatty():
        return (sys.stdin.readline().strip() or "q")[0].lower()
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1).lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _fmt_event(event: Mapping[str, object]) -> list[str]:
    bottle = str(event.get("bottle_label") or event.get("bottle_slug") or "?")
    arrival = str(event.get("arrival_timestamp") or "?")
    reason = str(event.get("reason") or "?")
    request_path = str(event.get("request_path") or "?")
    event_id = str(event.get("id") or "?")
    lines = [
        f"{_BOLD}{_RED}canary hit{_RESET}  bottle: {_BOLD}{bottle}{_RESET}",
        "",
        f"{_DIM}event:{_RESET}   {event_id}",
        f"{_DIM}when:{_RESET}    {arrival}",
        f"{_DIM}reason:{_RESET}  {reason}",
        f"{_DIM}path:{_RESET}    {request_path}",
    ]
    return lines


class CanaryReviewer:
    """Walks unresolved canary events, one at a time."""

    def __init__(
        self,
        client: OrchestratorClient,
        session: str = "",
        key: Callable[[], str] = read_key,
        out: Callable[[str], None] = print,
    ) -> None:
        self._client = client
        self._session = session
        self._key = key
        self._out = out
        self._skipped: set[str] = set()

    def _pending(self) -> list[dict[str, object]]:
        events = self._client.canary_events()
        return [
            e for e in events
            if str(e.get("id", "")) not in self._skipped
            and (not self._session or self._session in (
                str(e.get("bottle_slug", "")),
                str(e.get("bottle_label", "")),
            ))
        ]

    def run(self) -> int:
        while True:
            pending = self._pending()
            if not pending:
                self._out("no unresolved canary events")
                return 0
            result = self._review(pending[0], left=len(pending) - 1)
            if result == "q":
                return 0

    def _review(self, event: dict[str, object], *, left: int) -> str:
        self._out("\x1b[2J\x1b[H" + "\n".join(_fmt_event(event)))
        more = f"   {_DIM}({left} more waiting){_RESET}" if left else ""
        self._out(
            f"\n   [a] acknowledge   [t] terminate   [s] skip   [q] close{more}"
        )
        event_id = str(event.get("id", ""))
        bottle_slug = str(event.get("bottle_slug", ""))
        while True:
            key = self._key()
            if key in ("a", "t", "s", "q"):
                break
        if key == "s":
            self._skipped.add(event_id)
        elif key in ("a", "t"):
            action = (CANARY_ACTION_ACKNOWLEDGE if key == "a"
                      else CANARY_ACTION_TERMINATE)
            notes = ""
            if key == "t":
                sys.stdout.write(
                    f"\n{_DIM}notes (optional, press Enter to skip):{_RESET} "
                )
                sys.stdout.flush()
                try:
                    notes = sys.stdin.readline().strip()
                except (KeyboardInterrupt, EOFError):
                    notes = ""
            try:
                self._client.canary_resolve(
                    event_id,
                    bottle_slug=bottle_slug,
                    action=action,
                    notes=notes,
                )
                verb = "acknowledged" if action == CANARY_ACTION_ACKNOWLEDGE else "terminated"
                self._out(f"canary event {event_id}: {verb}")
            except OrchestratorClientError as e:
                self._out(f"couldn't resolve it: {e} (press a key)")
                self._key()
        return key


def cmd_resolve_canary(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        sys.stderr.write(__doc__ or "")
        return 0
    session = argv[0] if argv else ""
    try:
        url = env_orchestrator_url()
    except Exception as e:  # noqa: BLE001
        print(f"couldn't reach the orchestrator: {e}")
        return 1
    client = OrchestratorClient(url)
    try:
        return CanaryReviewer(client, session).run()
    except OrchestratorClientError as e:
        print(f"couldn't reach the orchestrator: {e}")
        return 1
