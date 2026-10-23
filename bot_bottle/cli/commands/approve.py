"""bot-bottle approve — answer an agent's requests (PRD 0094).

  bot-bottle approve [session]   review pending requests, one at a time

Opened by Ctrl-\\ in an agent's session when its status bar shows a
request. For each one: what the agent wants and why, then one key:

  a  allow once          p  allow for this project (saved in its bottle)
  d  deny                s  skip for now           q  close

"Allow for project" adds the route to the project's bottle
(`./.bot-bottle/bottles/<directory>.md`, extending `basic`), so the next run
starts with it. Requests that carry a credential can only be allowed once.
`bot-bottle supervise` lists every bottle's requests in one place.
"""

from __future__ import annotations

import sys
import termios
import tty
from collections.abc import Callable
from pathlib import Path

from ...approvals import Request, save_to_project, session_project
from ...control.client import OrchestratorClient, OrchestratorClientError, discover_orchestrator_url
from ..settings import settings_from_env

_DIM, _COPPER, _BOLD, _RESET = "\x1b[2m", "\x1b[38;5;173m", "\x1b[1m", "\x1b[0m"


def read_key() -> str:
    """One keypress, unbuffered (falls back to a line without a terminal)."""
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
    """What the request is, for a person deciding on it."""
    lines = [f"{_BOLD}{request.summary()}{_RESET}", ""]
    if request.justification.strip():
        lines += [f"{_DIM}why:{_RESET} {request.justification.strip()}", ""]
    routes = request.new_routes()
    if routes:
        lines.append(f"{_DIM}it would allow:{_RESET}")
        for route in routes:
            methods = sorted({m for entry in route.matches for m in entry.methods}) or ["any"]
            paths = sorted({p.value for entry in route.matches for p in entry.paths})
            extra = f"  paths {', '.join(paths)}" if paths else ""
            git = "  + git clone/fetch" if route.git_fetch else ""
            cred = f"  {_COPPER}with ${route.token_env}{_RESET}" if route.token_env else ""
            lines.append(f"  {route.host:<32} {', '.join(methods)}{extra}{git}{cred}")
    else:
        lines.append(f"{_DIM}proposed:{_RESET}")
        lines += [f"  {line}" for line in request.proposed.splitlines()[:20]]
    return lines


class Reviewer:  # pylint: disable=too-few-public-methods
    """Walks one session's pending requests (or every session's)."""

    def __init__(self, client: OrchestratorClient, session: str = "",
                 key: Callable[[], str] = read_key, out: Callable[[str], None] = print) -> None:
        self.client = client
        self.session = session
        self.key = key
        self.out = out
        self.skipped: set[str] = set()

    def pending(self) -> list[Request]:
        requests = [Request.from_dict(d) for d in self.client.supervise_pending()]
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
        project = session_project(request.session) if request.can_save_to_project() else None
        choices = ["[a] allow once"]
        if project is not None:
            choices.append("[p] allow for this project")
        choices += ["[d] deny", "[s] skip", "[q] close"]
        more = f"   {_DIM}({left} more waiting){_RESET}" if left else ""
        self.out("\n" + "   ".join(choices) + more)
        while True:
            key = self.key()
            if key in ("a", "d", "s", "q") or (key == "p" and project is not None):
                break
        try:
            if key in ("a", "p"):
                note = "allowed for the project" if key == "p" else "allowed once"
                self.client.supervise_respond(
                    request.id, bottle_slug=request.bottle, decision="approve",
                    notes=f"{note}, from the terminal")
                if key == "p" and project is not None:
                    saved = save_to_project(project, request.new_routes())
                    self.out(f"saved to {_short(saved)}")
            elif key == "d":
                self.client.supervise_respond(request.id, bottle_slug=request.bottle,
                                              decision="reject", notes="denied from the terminal")
            elif key == "s":
                self.skipped.add(request.id)
        except OrchestratorClientError as e:
            self.out(f"couldn't answer it: {e} (press a key)")
            self.key()
        return key


def _short(path: Path) -> str:
    try:
        return f"./{path.relative_to(Path.cwd())}"
    except ValueError:
        return str(path)


def cmd_approve(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        sys.stderr.write(__doc__ or "")
        return 0
    try:
        client = OrchestratorClient(discover_orchestrator_url(settings_from_env()))
        return Reviewer(client, argv[0] if argv else "").run()
    except OrchestratorClientError as e:
        print(f"couldn't reach the orchestrator: {e}")
        return 1
