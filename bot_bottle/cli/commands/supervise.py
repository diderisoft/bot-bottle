"""bot-bottle supervise: answer bottled agents' pending requests, one at a time.

  bot-bottle supervise [bottled agent]

Every bottled agent's requests, or just one's. Opened by Ctrl-\\ in a bottled
agent's terminal whose status bar shows a request. Keys:

  a  allow once          k  allow and keep in its bottle (the project's ./main,
                           or the bottle it launched with), from the next launch
  d  deny                s  skip for now           q  close

"Allow for project" adds the route to `./.bot-bottle/bottles/<directory>.md`
(extending `basic`). Requests carrying a credential can only be allowed once.
"""

from __future__ import annotations

import sys
import termios
import tty
from collections.abc import Callable, Mapping, Sequence

from ...approvals import (
    ALLOW_ONCE, ALLOW_PROJECT, DENY, Request, answer, keep_target, route_lines,
)
from ...control.client import (
    AlreadyAnswered, OrchestratorClient, OrchestratorClientError, pending_proposals,
)

_DECISION = {"a": ALLOW_ONCE, "k": ALLOW_PROJECT, "d": DENY}
_DIM, _COPPER, _BOLD, _RESET = "\x1b[2m", "\x1b[38;5;173m", "\x1b[1m", "\x1b[0m"


def read_key() -> str:
    """One keypress, unbuffered (a line without a terminal)."""
    if not sys.stdin.isatty():
        return (sys.stdin.readline().strip() or "q")[0].lower()
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1).lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def describe(request: Request) -> list[str]:
    """What the request is, for the person deciding."""
    lines = [f"{_BOLD}{request.summary()}{_RESET}", ""]
    if request.justification.strip():
        lines += [f"{_DIM}why:{_RESET} {request.justification.strip()}", ""]
    routes = route_lines(request, host_width=32,
                         credential=lambda env: f"{_COPPER}with ${env}{_RESET}")
    if routes:
        lines.append(f"{_DIM}it would allow:{_RESET}")
        lines += [f"  {line}" for line in routes]
    else:
        lines.append(f"{_DIM}proposed:{_RESET}")
        lines += [f"  {line}" for line in request.proposed.splitlines()[:20]]
    return lines


class Reviewer:  # pylint: disable=too-few-public-methods
    """Walks one session's pending requests, or every session's."""

    def __init__(self, fetch: Callable[[], Sequence[Mapping[str, object]]],
                 client_for: Callable[[str], OrchestratorClient], session: str = "",
                 key: Callable[[], str] = read_key, out: Callable[[str], None] = print) -> None:
        # Pending proposals from every orchestrator, and a client for the one
        # holding a request (its answer goes back there).
        self.fetch = fetch
        self.client_for = client_for
        self.session = session
        self.key = key
        self.out = out
        self.skipped: set[str] = set()

    def pending(self) -> list[Request]:
        requests = [Request.from_dict(dict(d)) for d in self.fetch()]
        return [r for r in requests if r.id not in self.skipped
                and (not self.session or r.session == self.session)]

    def run(self) -> int:
        while True:
            pending = self.pending()
            if not pending:
                self.out("nothing waiting")
                return 0
            if self.answer(pending[0], left=len(pending) - 1) == "q":
                return 0

    def answer(self, request: Request, *, left: int) -> str:
        """Show one request and act on the key pressed; returns the key."""
        self.out("\x1b[2J\x1b[H" + "\n".join(describe(request)))
        target = keep_target(request.session) if request.can_save_to_project() else None
        choices = ["[a] allow once"]
        if target is not None:
            choices.append(f"[k] allow and keep in {target.label()}")
        choices += ["[d] deny", "[s] skip", "[q] close"]
        more = f"   {_DIM}({left} more waiting){_RESET}" if left else ""
        self.out("\n" + "   ".join(choices) + more)
        while True:
            key = self.key()
            if key in ("a", "d", "s", "q") or (key == "k" and target is not None):
                break
        if key == "s":
            self.skipped.add(request.id)
        elif key in _DECISION:
            try:
                self.out(answer(self.client_for(request.orchestrator), request, _DECISION[key],
                                via="the terminal"))
            except AlreadyAnswered as e:
                self.out(f"already answered elsewhere: {e.decided_as}; yours wasn't applied "
                         "(press a key)")
                self.key()
            except (OrchestratorClientError, ValueError) as e:
                self.out(f"couldn't answer it: {e} (press a key)")
                self.key()
        return key


def cmd_supervise(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        sys.stderr.write(__doc__ or "")
        return 0
    try:
        return Reviewer(pending_proposals, OrchestratorClient,
                        argv[0] if argv else "").run()
    except OrchestratorClientError as e:
        print(f"couldn't reach the orchestrator: {e}")
        return 1
