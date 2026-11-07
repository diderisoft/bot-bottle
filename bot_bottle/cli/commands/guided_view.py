"""The guided `bot-bottle start` on the shared view (`screen.py`).

The same steps as `guided.py`, drawn as a step list beside the bottle, and
the launch's stages drawn under them on the same screen:

  ✓ Project    ~/Code/app  (gitea.dideric.is/acme/app)
  ✓ Agent      claude
  ✓ Task       fix the flaky test in tests/test_api.py
  ▸ Bottle     from the architect
      Builds on basic. Adds acme/app so the agent can fix the test.

      new  acme/app (push)

      d details
  enter run · s save as… · e edit · d details · q cancel

- **Agent.** None set up: Enter runs `setup` on this view, its lines under
  the step. Else the one named, the one remembered, the only
  logged-in one, or a menu.
- **Task.** Edited on its line; empty runs the agent in `basic`.
- **Bottle.** A spinner while the architect answers ("warming the
  architect…" while its bottle boots), then the proposal at a glance
  (`review.glance`): the architect's gist, what it adds, what to do first;
  `d` shows the card (`review_card`: what's new, each bottle it builds on,
  its forges) and every element (scrolled with ↑/↓) and its notes, `e` edits it in `$EDITOR`, `s` saves it
  as a bottle first, `q` cancels.
- **Launch.** The launch's stages, under these steps.

The questions on the way (trust a project bottle, confirm a host key, type a
secret, setup's sign-in) are asked inline, under the steps (`view_prompt`);
only `$EDITOR` takes the normal screen.

Only on the process's own terminal (`screen.usable`). Ctrl-C quits without
launching.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import TYPE_CHECKING

from ...contract.hostctl import LaunchRequest, Settings
from ...lib import log, prompt
from ...manifest.schema import DEFAULT_BOTTLE
from .. import screen, start_prefs, suggest_client
from . import review_card
from ..screen import CURRENT, DONE, FAILED, RUNNING, Screen, Step
from ..view_prompt import ViewPrompter
from . import guided

if TYPE_CHECKING:
    from ...manifest import ManifestIndex
    from ...suggest.proposal import Proposal

ARCHITECT = "bottle-architect"
HARNESS_NAMES = {"claude": "Claude Code", "codex": "Codex", "pi": "pi"}
REVIEW_KEYS = "enter run · s save as… · e edit · d details · q cancel"
BLOCKED_KEYS = "e edit · d details · q cancel"
WARM_POLL_SECONDS = 2.0  # how often the Bottle step re-asks whether the architect is up
# The launch callback: (request, index, view=, lead=) -> exit code.
Launch = Callable[..., int]


class Cancelled(Exception):
    """The user quit at a step."""


def home_relative(path: Path) -> str:
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def origin_of(cwd: Path) -> str:
    """The checkout's `origin` as host/owner/repo, or ""."""
    from ...onboarding import read_origin  # pylint: disable=import-outside-toplevel
    try:
        origin = read_origin(cwd)
    except (OSError, subprocess.SubprocessError):
        return ""
    return f"{origin.host}/{origin.owner}/{origin.repo}" if origin else ""


def project_value(cwd: Path) -> str:
    origin = origin_of(cwd)
    return f"{home_relative(cwd)}  ({origin})" if origin else home_relative(cwd)


class StartView:  # pylint: disable=too-many-instance-attributes
    """One guided start on `view`. `keys()` is the next key (tests feed
    their own)."""

    def __init__(self, view: Screen, args: argparse.Namespace, manifest: "ManifestIndex",  # pylint: disable=too-many-arguments
                 settings: Settings, *, launch: Launch,
                 keys: Callable[[float | None], str | None] | None = None,
                 cwd: Path | None = None) -> None:
        self.view = view
        self.args = args
        self.manifest = manifest
        self.settings = settings
        self.launch = launch
        self.keys = keys or _terminal_key
        self.cwd = cwd or Path(os.getcwd())
        self.style = view.style
        self.steps = [Step("Project", project_value(self.cwd), DONE)]
        self.warm_state = ""
        # What bot-bottle says while the view is up, drawn under the steps.
        self.notes: list[str] = []
        self._footer = ""  # the last frame's, for a redraw from another thread
        # Lines written to the terminal while the view was down.
        self._spoken: list[str] = []
        # A question being asked (`lib.prompt`), drawn under the steps.
        self.asking: list[str] = []
        self.asker = ViewPrompter(self._show_asking, self.key, width=self.width,
                                  style=self.style, suspend=self.suspended)

    # --- drawing ---

    def width(self) -> int:
        columns, _ = screen.size()
        art = screen.GAP + screen.ART_WIDTH if self.view.art else 0
        return max(min(72, columns - 2 * screen.MARGIN - art), 30)

    def body_width(self) -> int:
        """Room for the current step's body, under its 4-column indent."""
        return self.width() - 4

    def draw(self, footer: str = "") -> None:
        self._footer = footer
        lines = screen.step_lines(self.steps, width=self.width(), style=self.style,
                                  frame=self.view.frame)
        if self.notes:
            lines.append("")
            for note in self.notes[-3:]:
                lines += [self.style.amber(w) for w in screen.wrap(note, self.width())[:2]]
        if self.asking:
            lines += [""] + self.asking
        self.view.draw(lines, footer=footer, left_width=self.width())

    def _show_asking(self, lines: list[str] | None, footer: str) -> None:
        self.asking = list(lines or [])
        if lines is not None:
            self.draw(footer)

    def note(self, line: str) -> None:
        line = line.removeprefix("bot-bottle: ")
        if line not in self.notes:
            self.notes.append(line)

    @contextlib.contextmanager
    def suspended(self) -> Generator[None]:
        """The normal screen, with bot-bottle's output going to it again."""
        # Its questions ask there too.
        with self.view.suspend(), log.redirect(_to_stderr), prompt.redirect(None):
            yield

    def key(self, timeout: float | None = None) -> str | None:
        return self.keys(timeout)

    def current(self, title: str, value: str = "") -> Step:
        step = Step(title, value, CURRENT)
        self.steps.append(step)
        return step

    # --- the flow ---

    def run(self) -> int:
        threading.Thread(target=self._warm, daemon=True).start()
        try:
            with log.redirect(self.note), prompt.redirect(self.asker):
                return self._run()
        except Cancelled:
            self.view.leave()
            return 0
        except log.Die as e:
            # Its message went to the view's notes, which go with it; say it
            # to whoever started this, unless it already reached the terminal.
            self.view.leave()
            if e.message and not any(e.message in line for line in self._spoken):
                log.error(e.message)
            raise
        except KeyboardInterrupt:
            self.view.leave()
            return 130

    def _run(self) -> int:
        agent = self.agent_step()
        task = self.task_step(agent)
        base = LaunchRequest(agent=agent, bottles=(DEFAULT_BOTTLE,),
                             label=self.args.label or agent, color=self.args.color or "",
                             image_policy="prefer-cached", settings=self.settings,
                             cwd=str(self.cwd), copy_cwd=self.args.cwd)
        if not task:
            return self._launch(base, self.manifest)
        proposal, saved_as = self.review_step(self.suggest_step(task))
        # pylint: disable-next=import-outside-toplevel
        from ...run_bottle import index_for
        bottles, run_text = guided.bottles_for(proposal, saved_as=saved_as)
        request = dataclasses.replace(base, bottles=bottles, prompt=task, run_bottle=run_text)
        return self._launch(request, index_for(self.manifest, text=run_text))

    def _launch(self, request: LaunchRequest, index: "ManifestIndex") -> int:
        """The launch, its stages under these steps; its output as before."""
        with log.redirect(self._say):
            return self.launch(request, index, view=self.view, lead=self.steps)

    def _say(self, line: str) -> None:
        """A launch's line: a note while the view is up (a raw write would
        scroll it and garble the frames), the terminal once it's handed back."""
        if self.view.on:
            self.note(line)
        else:
            self._spoken.append(line)
            _to_stderr(line)

    def _warm(self) -> None:
        """Ask the host to warm the architect. What that says on the way
        ("restarting the host controller") shows only while it runs; a
        warning or error stays."""
        progress: list[str] = []

        def say(line: str) -> None:
            self.note(line)
            if not _lasting(line):
                progress.append(line.removeprefix("bot-bottle: "))
            self._redraw()

        with log.redirect(say):
            self.warm_state = suggest_client.warm(self.settings) or ""
        if progress:
            self.notes[:] = [n for n in self.notes if n not in progress]
            self._redraw()

    def _redraw(self) -> None:
        """Draw the current frame again, from a background thread, while the
        view is up."""
        if self.view.on:
            self.draw(self._footer)

    # --- agent ---

    def agents(self) -> list[str]:
        return [a for a in self.manifest.all_agent_names if a != ARCHITECT]

    def agent_step(self) -> str:
        if not self.agents():
            self.setup_step()
        named = self.args.name
        if named:
            self.manifest.require_agent(named)
            start_prefs.remember_agent(named)
            return self.done("Agent", named)
        remembered = start_prefs.remembered_agent()
        if remembered in self.agents():
            return self.done("Agent", remembered)
        ready = guided.logged_in_harnesses(self.agents())
        choices = ready or self.agents()
        picked = choices[0] if len(choices) == 1 else self.pick_agent(choices, set(ready))
        start_prefs.remember_agent(picked)
        return self.done("Agent", picked)

    def setup_step(self) -> None:
        """No agents: Enter runs `setup` here, its lines under this step and
        its questions inline."""
        # pylint: disable-next=import-outside-toplevel
        from ...agent_provider import get_provider
        found = [HARNESS_NAMES.get(h, h) for h in guided.HARNESSES
                 if get_provider(h).detect_host(os.environ).logged_in]
        step = self.current("Agents", "none set up yet")
        step.body = ([f"found {', '.join(found)} (logged in)"] if found
                     else ["no logged-in Claude Code, Codex or pi found"])
        step.body += ["", f"{self.style.accent('›')} set up agents"]
        while True:
            self.draw("enter set up · q quit")
            key = self.key()
            if key in ("q", "esc", "ctrl-c"):
                raise Cancelled
            if key == "enter":
                break
        out: list[str] = []

        def say(text: str) -> None:
            out.extend(line.removeprefix("bot-bottle: ") for line in text.split("\n"))
            step.body = [row for line in out for row in screen.wrap(line, self.body_width())][-8:]
            self.draw("setting up…")

        step.value, step.body = "setting up", []
        self.draw("setting up…")
        with log.redirect(say):  # its questions: this view's (`run`)
            from .setup import cmd_setup  # pylint: disable=import-outside-toplevel
            cmd_setup(["--no-pair"])
        # pylint: disable-next=import-outside-toplevel
        from ...manifest import ManifestIndex
        self.manifest = ManifestIndex.resolve(str(self.cwd))
        self.steps.pop()
        if not self.agents():
            self.steps.append(Step("Agents", "none set up", FAILED, body=step.body))
            self.draw("q quit")
            while self.key() not in ("q", "esc", "enter", "ctrl-c"):
                pass
            raise Cancelled
        self.steps.append(Step("Agents", ", ".join(self.agents()), DONE))

    def pick_agent(self, choices: list[str], ready: set[str]) -> str:
        cursor = 0
        step = self.current("Agent")
        while True:
            items = [(c, f"{HARNESS_NAMES.get(c, c)}" + (" · logged in" if c in ready else ""))
                     for c in choices]
            step.body = screen.menu_lines(items, cursor, width=self.width() - 4,
                                          style=self.style, label_width=12)
            self.draw("↑↓ move · enter choose · q quit")
            key = self.key()
            if key in ("up", "k"):
                cursor = (cursor - 1) % len(choices)
            elif key in ("down", "j"):
                cursor = (cursor + 1) % len(choices)
            elif key == "enter":
                self.steps.pop()
                return choices[cursor]
            elif key in ("q", "esc"):
                raise Cancelled

    def done(self, title: str, value: str) -> str:
        self.steps.append(Step(title, value, DONE))
        return value

    # --- task ---

    def task_step(self, agent: str) -> str:
        if self.args.prompt is not None:
            if self.args.prompt:
                self.done("Task", self.args.prompt)
            return self.args.prompt
        text = ""
        step = self.current("Task")
        hint = f"enter suggest a bottle · empty: just open {agent} in {DEFAULT_BOTTLE} · esc quit"
        while True:
            step.body = screen.input_lines(f"{self.style.accent('›')} ", text, self.body_width())
            self.draw(hint)
            key = self.key()
            if key == "enter":
                break
            if key == "esc":
                raise Cancelled
            text = screen.edit_line(text, key or "")
        self.steps.pop()
        text = text.strip()
        self.done("Task", text or f"none: open {agent} in {DEFAULT_BOTTLE}")
        return text

    # --- suggestion ---

    def suggest_step(self, task: str) -> "Proposal":
        result: list["Proposal"] = []
        failure: list[BaseException] = []

        def ask() -> None:
            try:
                with log.redirect(self.note):
                    result.append(suggest_client.suggest(task, self.cwd, settings=self.settings))
            except BaseException as e:  # pylint: disable=broad-exception-caught
                failure.append(e)

        def follow_warm() -> None:
            # The label tracks the bottle: "warming" only while it boots.
            while worker.is_alive() and self.warm_state == "booting":
                worker.join(WARM_POLL_SECONDS)
                if worker.is_alive():
                    self._warm()

        worker = threading.Thread(target=ask, daemon=True)
        worker.start()
        threading.Thread(target=follow_warm, daemon=True).start()
        step = Step("Bottle", "", RUNNING, started=time.monotonic())
        self.steps.append(step)
        while worker.is_alive():
            step.value = ("warming the architect…" if self.warm_state == "booting"
                          else "asking the architect…")
            self.view.frame += 1
            self.draw("ctrl-c quit")
            worker.join(0.1)
        self.steps.pop()
        if failure:
            raise failure[0]
        return result[0]

    # --- review ---

    def review_step(self, proposal: "Proposal") -> tuple["Proposal", str]:
        # pylint: disable=import-outside-toplevel
        from ...paths import bot_bottle_root
        from ...suggest.flow import bottle_dirs
        from ...suggest.write import SuggestWriteError, write_bottle
        dirs = bottle_dirs(self.cwd, bot_bottle_root())
        details, error, offset = False, "", 0
        step = self.current("Bottle")
        while True:
            step.value = f"from the {proposal.source}" if proposal.source else ""
            body, offset = review_lines(proposal, dirs, details=details, width=self.body_width(),
                                        style=self.style, offset=offset,
                                        height=screen.size()[1] - len(self.steps) - 8)
            step.body = body + ([self.style.red(error)] if error else [])
            self.draw(BLOCKED_KEYS if proposal.blocked else REVIEW_KEYS)
            key = self.key()
            if key == "enter" and not proposal.blocked:
                self.steps[-1] = Step("Bottle", bottle_summary(proposal), DONE)
                return proposal, ""
            if key == "s" and not proposal.blocked:
                name = self.ask_line(step, "save as")
                if not name:
                    continue
                try:
                    write_bottle(name, proposal)
                except SuggestWriteError as e:
                    error = str(e)
                    continue
                self.steps[-1] = Step("Bottle", f"{name} (saved)", DONE)
                return proposal, name
            if key == "e":
                with self.suspended():
                    edited = guided.edit_proposal(proposal, dirs)
                if edited is not None:
                    proposal, error = edited, ""
            elif key == "d":
                details, offset = not details, 0
            elif details and key in ("up", "k", "down", "j"):
                offset += -1 if key in ("up", "k") else 1
            elif key in ("q", "esc"):
                raise Cancelled

    def ask_line(self, step: Step, label: str) -> str:
        """One line typed under `step`'s body; "" on esc."""
        text, body = "", list(step.body)
        while True:
            step.body = body + [""] + screen.input_lines(
                f"{label} {self.style.accent('›')} ", text, self.body_width())
            self.draw("enter save · esc back")
            key = self.key()
            if key == "enter":
                step.body = body
                return text.strip()
            if key == "esc":
                step.body = body
                return ""
            text = screen.edit_line(text, key or "")


def bottle_summary(proposal: "Proposal") -> str:
    """`basic + gitea, adds 1`, or the new bottle's name."""
    add = proposal.additions
    added = len(add.egress) + len(add.repos) + len(add.env) + len(add.credentials)
    if proposal.new_bottle and proposal.new_bottle.name:
        base = f"new {proposal.new_bottle.name}"
        if proposal.new_bottle.extends:
            base += f" (extends {proposal.new_bottle.extends})"
    else:
        base = " + ".join(proposal.bottles) or DEFAULT_BOTTLE
    source = f"  · from the {proposal.source}" if proposal.source else ""
    return base + (f", adds {added}" if added else "") + source


def review_lines(proposal: "Proposal", dirs: list[tuple[Path, bool]], *, details: bool,
                 width: int, style: screen.Style, offset: int = 0,
                 height: int = 0) -> tuple[list[str], int]:
    """The proposal for the review step, and the details' scroll offset
    (clamped). At a glance: the gist, what it adds and what to do first
    (`review.glance`). `details`: the card (`review.review`), then every
    element under its bottle, `height` lines of them from `offset`, and the
    notes."""
    # pylint: disable-next=import-outside-toplevel
    from ...suggest import render, review
    if details:
        lines = _details(proposal, dirs, width, style)
        notes = [style.dim(w) for note in proposal.notes
                 for w in screen.wrap(f"note: {note}", width)]
        lines, offset = review_card.window(lines, offset, max(height - len(notes) - 1, 4),
                                           style=style)
        lines += [""] + notes if notes else []
    else:
        seen = review.glance(proposal, dirs)
        lines = screen.wrap(seen.gist if seen else render.summary_line(proposal), width)[:3]
        lines.append("")
        if seen is None:
            lines.append(style.dim("(the bottles couldn't be read)"))
        else:
            lines += _hanging("new", " · ".join(seen.new) or "nothing new", width,
                              paint_label=style.amber,
                              paint=str if seen.new else style.dim)
            for warn in seen.warns:
                lines += _hanging("⚠", warn, width,
                                  paint_label=style.amber, paint=style.amber)
        n = len(proposal.notes)
        lines += ["", style.dim("d details" + (f" · {n} note{'s' if n != 1 else ''}"
                                                if n else ""))]
    if proposal.blocked:
        lines += [style.red(w) for w in screen.wrap(f"can't run: {proposal.blocked}", width)]
    return lines, offset


_LABEL = 5  # "new  ", "⚠    "


def _hanging(label: str, text: str, width: int, *, paint_label: Callable[[str], str],
             paint: Callable[[str], str]) -> list[str]:
    """`text` after `label`, its wrapped lines under the text."""
    rows = screen.wrap(text, max(width - _LABEL, 8)) or [""]
    return ([paint_label(label.ljust(_LABEL)) + paint(rows[0])]
            + [" " * _LABEL + paint(r) for r in rows[1:]])


def _details(proposal: "Proposal", dirs: list[tuple[Path, bool]], width: int,
             style: screen.Style) -> list[str]:
    """The card, then every element under its bottle."""
    # pylint: disable-next=import-outside-toplevel
    from ...suggest import review
    card, every = review.review(proposal, dirs), review.every_line(proposal, dirs)
    if card is None or every is None:
        return [style.dim("(the bottles couldn't be read)")]
    return (review_card.card(card, width=width, style=style) + [""]
            + review_card.card(every, width=width, style=style))


def _lasting(line: str) -> bool:
    """A warning or an error, which a note keeps; anything else is progress."""
    return any(f"{level}:" in line for level in ("warning", "error"))


def _to_stderr(line: str) -> None:
    sys.stderr.write(line + "\n")


def _terminal_key(timeout: float | None = None) -> str | None:
    with screen.keys_mode():
        return screen.read_key(timeout=timeout)


def run_start(args: argparse.Namespace, manifest: "ManifestIndex", settings: Settings, *,
              launch: Launch, view: Screen | None = None) -> int:
    """The guided start on `view` (a new one by default, closed after)."""
    own = view or Screen()
    try:
        return StartView(own, args, manifest, settings, launch=launch).run()
    finally:
        if view is None:
            own.close()


__all__ = ["StartView", "bottle_summary", "review_lines", "run_start"]
