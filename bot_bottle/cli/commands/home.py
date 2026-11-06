"""Bare `bot-bottle`: home, on the bottle view (`screen.py`).

  bot-bottle · delphi
  ✓ host ready · firecracker · console linked

  › Start a bottled agent
    Agents            2 running · 1 needs you
    Approvals         1 waiting
    Host              status, services
    Set up agents     claude ✓ codex ✓ pi –

A status strip says what the host has or lacks (agents, the services, the
backend, the console link) and the cursor starts on the fix: Set up agents
when there are none, Host (start the services) when they're down. A backend
that isn't ready gets the command that sets it up (`bot-bottle install`,
which prints the sudo lines); home never runs sudo.

- **Start a bottled agent**: the guided start (`guided_view`), on this view.
- **Agents**: running ones first. Enter attaches (Ctrl-] comes back here),
  `r` resumes a stopped one, `x` stops a running one, `n` renames.
- **Set up agents**: `setup`, on this view: its lines under the menu, its
  questions inline (`view_prompt`), then Enter comes back.
- **Approvals**: `supervise`. **Host**: `up` when the services are down,
  else `status`. These run on the normal screen and come back.

Only bare and on the process's own terminal; without one, bare `bot-bottle`
prints `status` and a hint. `bot-bottle <agent>` starts that agent as
before.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import socket
import sys
import threading
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path

from ...lib import log, prompt
from .. import screen
from ..constants import PROG
from ..screen import Screen
from ..view_prompt import ViewPrompter
from .guided import HARNESSES

ARCHITECT = "bottle-architect"


@dataclass
class Bot:
    """One bottled agent, for the Agents list."""

    id: str
    agent: str
    label: str
    running: bool
    needs_you: bool = False


@dataclass
class Snapshot:  # pylint: disable=too-many-instance-attributes
    """What home shows about this host."""

    agents: list[str] = field(default_factory=list)
    services: bool = False
    backend: str = ""
    backend_ready: bool = True
    console: str = ""
    bots: list[Bot] = field(default_factory=list)
    approvals: int = 0
    starters: dict[str, bool] = field(default_factory=dict)

    def missing(self) -> list[str]:
        out = []
        if not self.agents:
            out.append("no agents set up")
        if not self.services:
            out.append("services not running")
        if self.backend and not self.backend_ready:
            out.append(f"backend {self.backend} not ready")
        if not self.backend:
            out.append("no backend")
        return out


def take_snapshot(cwd: Path) -> Snapshot:  # pylint: disable=too-many-locals
    """The host as home shows it. Best effort: a part that can't be read
    reads as missing."""
    # pylint: disable=import-outside-toplevel
    from ...backend.selection import is_backend_ready
    from ...control.hostctl import runner
    from ...manifest import ManifestError, ManifestIndex
    from ..settings import settings_from_env
    snap = Snapshot()
    settings = settings_from_env()
    try:
        index = ManifestIndex.resolve(str(cwd), missing_ok=True)
        snap.agents = [a for a in index.all_agent_names if a != ARCHITECT]
    except (ManifestError, OSError):
        pass
    snap.starters = {h: h in snap.agents for h in HARNESSES}
    state = runner.running()
    snap.services = state is not None
    snap.backend = backend_name(settings)
    if snap.backend:
        with contextlib.suppress(Exception):
            snap.backend_ready = is_backend_ready(settings, snap.backend, quiet=True)
    snap.console = console_state()
    snap.bots = list_bots()
    if snap.services:
        with contextlib.suppress(Exception):
            from ...control.client import pending_proposals
            pending = pending_proposals()
            snap.approvals = len(pending)
            waiting = {str(p.get("bottle_slug") or "") for p in pending}
            for bot in snap.bots:
                bot.needs_you = bot.id in waiting
    return snap


def backend_name(settings) -> str:  # type: ignore[no-untyped-def]
    """The backend a launch would use, without prompting; "" when none."""
    # pylint: disable-next=import-outside-toplevel
    from ...backend.selection import auto_backend_name
    with contextlib.suppress(Exception):
        return auto_backend_name(settings)
    return ""


def console_state() -> str:
    """`console linked`, `console not connected`, or "" when unpaired."""
    # pylint: disable=import-outside-toplevel
    with contextlib.suppress(Exception):
        from ...control.hostctl import pairing
        if not pairing.load_config().enabled:
            return ""
        status = pairing.read_status() or {}
        return "console linked" if status.get("connected") else "console not connected"
    return ""


def list_bots() -> list[Bot]:
    """Running bottled agents first, then stopped ones, newest first."""
    # pylint: disable=import-outside-toplevel
    from . import bottled_agents
    bots: list[Bot] = []
    with contextlib.suppress(Exception):
        running, _ = bottled_agents._running()  # pylint: disable=protected-access
        bots += [Bot(r[0], r[1], r[2] or r[0], True) for r in running]
        _, stopped = bottled_agents.stopped_table()
        bots += [Bot(r[0], r[1], r[2] or r[0], False) for r in stopped]
    # The architect's own bottle is the host's, not one of yours.
    return [b for b in bots if b.agent != ARCHITECT]


def window(total: int, cursor: int, rows: int) -> tuple[int, int]:
    """(first, count) of a `rows`-tall window over `total` items that keeps
    `cursor` in view."""
    rows = max(min(rows, total), 1)
    first = min(max(cursor - rows // 2, 0), max(total - rows, 0))
    return first, rows


def stop_bot(bot_id: str) -> None:
    """Tear down a running bottled agent through the host controller."""
    # pylint: disable-next=import-outside-toplevel
    from .cleanup import _teardown_held
    for failure in _teardown_held([bot_id]):  # pylint: disable=protected-access
        log.write_line(f"bot-bottle: {failure}")


def _run_command(name: str, argv: list[str]) -> int:
    # pylint: disable-next=import-outside-toplevel
    from . import COMMANDS
    return COMMANDS[name](argv) or 0


def _pause() -> None:
    sys.stderr.write("\n(press Enter to go back) ")
    sys.stderr.flush()
    with contextlib.suppress(EOFError):
        sys.stdin.readline()


class Home:  # pylint: disable=too-many-instance-attributes
    """The home menu on `view`. `keys`, `snapshot` and `run` are injectable
    for tests."""

    def __init__(self, view: Screen, *, keys: Callable[[float | None], str | None] | None = None,
                 snapshot: Callable[[], Snapshot] | None = None,
                 run: Callable[[str, list[str]], int] | None = None,
                 start: Callable[[], int] | None = None, cwd: Path | None = None) -> None:
        self.view = view
        self.style = view.style
        self.cwd = cwd or Path(os.getcwd())
        self.keys = keys or _terminal_key
        self.snapshot_fn = snapshot or (lambda: take_snapshot(self.cwd))
        self.run_command = run or _run_command
        self.start_fn = start or self._start
        self.snap: Snapshot | None = None
        self.notes: list[str] = []

    # --- drawing ---

    def width(self) -> int:
        columns, _ = screen.size()
        art = screen.GAP + screen.ART_WIDTH if self.view.art else 0
        return max(min(64, columns - 2 * screen.MARGIN - art), 30)

    def strip_lines(self) -> list[str]:
        title = self.style.bold(f"bot-bottle · {socket.gethostname().split('.')[0]}")
        if self.snap is None:
            return [title, self.style.dim("checking the host…")]
        missing = self.snap.missing()
        if not missing:
            parts = ["host ready", self.snap.backend] + ([self.snap.console]
                                                         if self.snap.console else [])
            return [title, f"{self.style.green('✓')} {' · '.join(parts)}"]
        width = self.width()
        status = screen.wrap(" · ".join(missing), width - 2)
        lines = [title, f"{self.style.red('✗')} {status[0]}"] + [f"  {w}" for w in status[1:]]
        if self.snap.backend and not self.snap.backend_ready:
            hint = (f"set it up with `{PROG} install --backend={self.snap.backend}` "
                    "(it prints the sudo lines to run)")
            lines += [self.style.dim(f"  {w}") for w in screen.wrap(hint, width - 2)]
        return lines

    def items(self) -> list[tuple[str, str, str]]:
        """(key, label, detail) per menu entry."""
        snap = self.snap or Snapshot(services=True)
        running = [b for b in snap.bots if b.running]
        needs = sum(1 for b in running if b.needs_you)
        agents = (f"{len(running)} running" + (f" · {needs} needs you" if needs else "")
                  if running else "none running")
        stopped = len(snap.bots) - len(running)
        if stopped:
            agents += f" · {stopped} stopped"
        starters = " ".join(f"{h} {'✓' if ok else '–'}" for h, ok in snap.starters.items())
        return [
            ("start", "Start a bottled agent", "" if snap.agents or self.snap is None
             else "(set up an agent first)"),
            ("agents", "Agents", agents),
            ("approvals", "Approvals", f"{snap.approvals} waiting" if snap.approvals else "none"),
            ("host", "Host", "start services" if not snap.services else "status"),
            ("setup", "Set up agents", starters),
        ]

    def draw(self, body: list[str], footer: str) -> None:
        lines = self.strip_lines() + [""] + body
        if self.notes:
            lines.append("")
            lines += [self.style.amber(screen.fit(n, self.width())) for n in self.notes[-3:]]
        self.view.draw(lines, footer=footer, left_width=self.width(), align="center")

    def note(self, line: str) -> None:
        line = line.removeprefix("bot-bottle: ")
        if line not in self.notes:
            self.notes.append(line)

    # --- the loop ---

    def run(self) -> int:
        try:
            with log.redirect(self.note):
                return self._loop()
        except KeyboardInterrupt:
            return 130
        finally:
            self.view.close()

    def refresh(self) -> None:
        """Take a new snapshot in the background, redrawing when it lands."""
        done = threading.Event()

        def take() -> None:
            with log.redirect(self.note):
                self.snap = self.snapshot_fn()
            done.set()

        threading.Thread(target=take, daemon=True).start()
        done.wait(0.05)

    def default_cursor(self) -> int:
        keys = [k for k, _, _ in self.items()]
        if self.snap is not None and not self.snap.agents:
            return keys.index("setup")
        if self.snap is not None and not self.snap.services:
            return keys.index("host")
        return keys.index("start")

    def _loop(self) -> int:
        self.refresh()
        cursor, placed = 0, False
        while True:
            if self.snap is not None and not placed:
                cursor, placed = self.default_cursor(), True
            items = self.items()
            body = screen.menu_lines([(label, detail) for _, label, detail in items], cursor,
                                     width=self.width(), style=self.style)
            self.draw(body, "↑↓ move · enter open · q quit")
            key = self.keys(0.2)
            if key is None:
                continue
            if key in ("up", "k"):
                cursor = (cursor - 1) % len(items)
            elif key in ("down", "j"):
                cursor = (cursor + 1) % len(items)
            elif key in ("q", "esc"):
                return 0
            elif key in ("enter", "right"):
                self.open(items[cursor][0])
                self.refresh()

    def open(self, entry: str) -> None:
        snap = self.snap or Snapshot()
        if entry == "start":
            self.start_fn()
        elif entry == "agents":
            self.agents_menu()
        elif entry == "approvals":
            self.on_normal_screen("supervise", [])
        elif entry == "host":
            if not snap.services:
                self.on_normal_screen("up", [])
            else:
                self.on_normal_screen("status", [], pause=True)
        elif entry == "setup":
            self.in_view("Set up agents", "setup", [])
            self.refresh_now()

    def on_normal_screen(self, name: str, argv: list[str], *, pause: bool = False) -> int:
        """Run a command on the normal screen, then come back."""
        with self.view.suspend(), log.redirect(_to_stderr):
            try:
                code = self.run_command(name, argv)
            except SystemExit as e:  # a command's die()
                code = e.code if isinstance(e.code, int) else 1
            if pause:
                _pause()
        return code

    def in_view(self, title: str, name: str, argv: list[str]) -> int:
        """Run a command on this view: its lines under `title`, its questions
        asked inline (`lib.prompt`), then a key to come back."""
        out: list[str] = []
        asking: list[str] = []

        def body() -> list[str]:
            room = max(screen.size()[1] - len(self.strip_lines()) - len(asking) - 10, 3)
            rows = [row for line in out for row in screen.wrap(line, self.width())]
            shown = rows[-room:] if len(rows) > room else rows
            return [self.style.bold(title), ""] + shown + asking

        def say(text: str) -> None:
            out.extend(line.removeprefix("bot-bottle: ") for line in text.split("\n"))
            self.draw(body(), "working…")

        def show(lines: list[str] | None, footer: str) -> None:
            asking[:] = [""] + lines if lines else []
            if lines is not None:
                self.draw(body(), footer)

        asker = ViewPrompter(show, lambda: self.keys(None), width=self.width,
                             style=self.style, suspend=self._outside)
        self.draw(body(), "working…")
        with log.redirect(say), prompt.redirect(asker):
            try:
                code = self.run_command(name, argv)
            except SystemExit as e:  # a command's die()
                code = e.code if isinstance(e.code, int) else 1
        while True:
            self.draw(body(), "enter back")
            if self.keys(None) in ("enter", "esc", "q", "left"):
                return code

    @contextlib.contextmanager
    def _outside(self) -> Generator[None]:
        """The normal screen, with bot-bottle's output going to it again."""
        with self.view.suspend(), log.redirect(_to_stderr):
            yield
        self.view.invalidate()

    # --- agents ---

    def agents_menu(self) -> None:  # pylint: disable=too-many-branches
        cursor = 0
        while True:
            bots = (self.snap or Snapshot()).bots
            if not bots:
                self.draw([self.style.dim("no bottled agents yet")], "← back")
                if self.keys(None) in ("left", "esc", "q", "enter"):
                    return
                continue
            cursor = min(cursor, len(bots) - 1)
            items = [(f"{b.agent}  {screen.fit(b.label, 24)}",
                      ("running" if b.running else "stopped")
                      + (" · needs you" if b.needs_you else "")) for b in bots]
            first, shown = window(len(items), cursor, max(screen.size()[1] - 14, 3))
            lines = screen.menu_lines(items[first:first + shown], cursor - first,
                                      width=self.width(), style=self.style, label_width=34)
            more = [self.style.dim(f"  … {len(items) - first - shown} more")] \
                if first + shown < len(items) else []
            body = [self.style.bold("Agents") + self.style.dim(f"  {cursor + 1}/{len(items)}"),
                    ""] + lines + more
            bot = bots[cursor]
            footer = ("enter attach · x stop · n rename · ← back" if bot.running
                      else "r resume · n rename · ← back")
            self.draw(body, footer)
            key = self.keys(None)
            if key in ("up", "k"):
                cursor = (cursor - 1) % len(bots)
            elif key in ("down", "j"):
                cursor = (cursor + 1) % len(bots)
            elif key in ("left", "esc", "q"):
                return
            elif key == "enter" and bot.running:
                self.on_normal_screen("attach", [bot.id])
                self.refresh_now()
            elif key == "r" and not bot.running:
                self.on_normal_screen("resume", [bot.id])
                self.refresh_now()
            elif key == "x" and bot.running and self.confirm(body, f"stop {bot.label}?"):
                with self.view.suspend(), log.redirect(_to_stderr):
                    stop_bot(bot.id)
                self.refresh_now()
            elif key == "n":
                label = self.ask(body, f"new label for {bot.label}")
                if label:
                    self.on_normal_screen("rename", [bot.id, label])
                    self.refresh_now()

    def refresh_now(self) -> None:
        with log.redirect(self.note):
            self.snap = self.snapshot_fn()

    def confirm(self, body: list[str], question: str) -> bool:
        self.draw(body + ["", f"{question}  y/n"], "y yes · n no")
        return self.keys(None) == "y"

    def ask(self, body: list[str], label: str) -> str:
        text = ""
        while True:
            self.draw(body + [""] + screen.input_lines(f"{label} {self.style.accent('›')} ",
                                                       text, self.width()),
                      "enter save · esc back")
            key = self.keys(None)
            if key == "enter":
                return text.strip()
            if key == "esc":
                return ""
            text = screen.edit_line(text, key or "")

    # --- start ---

    def _start(self) -> int:
        """The guided start on this view; back here when the agent ends or
        detaches."""
        # pylint: disable=import-outside-toplevel
        from ...manifest import ManifestIndex
        from ..settings import settings_from_env
        from .guided_view import StartView
        from .start import _launch_bottle

        def go(request, index, *, view=None, lead=None):  # type: ignore[no-untyped-def]
            return _launch_bottle(request, index, dry_run=False, view=view, lead=lead)

        args = argparse.Namespace(name=None, prompt=None, label=None, color=None, cwd=False)
        manifest = ManifestIndex.resolve(str(self.cwd), missing_ok=True)
        # A failed start's error comes back here as a note on this menu.
        with log.redirect(self.note):
            try:
                return StartView(self.view, args, manifest, settings_from_env(),
                                 launch=go, keys=self.keys, cwd=self.cwd).run()
            except SystemExit as e:
                return e.code if isinstance(e.code, int) else 1
            finally:
                self.view.invalidate()


def _to_stderr(line: str) -> None:
    sys.stderr.write(line + "\n")


def _terminal_key(timeout: float | None = None) -> str | None:
    with screen.keys_mode():
        return screen.read_key(timeout=timeout)


def cmd_home(argv: list[str]) -> int:
    """Bare `bot-bottle`."""
    if argv and argv[0] in ("-h", "--help"):
        sys.stderr.write(__doc__ or "")
        return 0
    if not screen.usable():
        code = _run_command("status", [])
        print(f"\n`{PROG} start` launches an agent here; on a terminal, `{PROG}` "
              f"opens the home menu. `{PROG} help` lists commands.")
        return code
    return Home(Screen()).run()


__all__ = ["Bot", "Home", "Snapshot", "cmd_home", "take_snapshot", "window"]
