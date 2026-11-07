"""The guided `bot-bottle start` (PRD 0117): pick a harness, describe the
task, get a suggested bottle, optionally edit it, then run the agent on it.

Only on a terminal, and only without `--bottle` (`bot-bottle start claude
--bottle web` launches as before). The architect's warm bottle starts
booting first, so it boots while you choose and type.

1. **Harness.** The one you last ran (`start_prefs`), else a pick among the
   logged-in harnesses that have an agent. A positional agent
   (`bot-bottle start codex`) runs that one and becomes the default.
2. **Task.** Empty runs the agent in `basic`, as a plain start.
3. **Suggest** (`suggest_client`, the shared flow) with a spinner, shown as
   `bottles suggest` shows it.
4. **Confirm**, on one line under the proposal so it stays on screen: run
   it, save it as a named bottle first, edit it (the proposal in `$EDITOR`,
   reopened with the errors inline until it validates), or cancel. A
   proposal that can't run here (`Proposal.blocked`) can only be edited or
   cancelled, and `--auto` refuses it.
5. **Launch** in it, with the task as the agent's first prompt.

An unsaved suggestion runs as this run's own bottle (`run_bottle`, ADR
0017): recorded with the run, never added to the library.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import shlex
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from ...contract.hostctl import RUN_BOTTLE, LaunchRequest, Settings
from ...lib.log import info
from ...manifest.schema import DEFAULT_BOTTLE
from ...util import read_tty_line
from .. import start_prefs, suggest_client, tui

if TYPE_CHECKING:
    from ...manifest import ManifestIndex
    from ...suggest.proposal import Proposal

HARNESSES = ("claude", "codex", "pi")
RUN, SAVE, EDIT, CANCEL = "Run it", "Save it as a bottle, then run it", "Edit it", "Cancel"
CONFIRM_PROMPT = "Use this bottle?  [Enter] run  [s] save as…  [e] edit  [q] cancel "
BLOCKED_PROMPT = "It can't run here.  [e] edit  [q] cancel "
_KEYS = {"\n": RUN, "\r": RUN, "r": RUN, "s": SAVE, "e": EDIT, "q": CANCEL, "\x1b": CANCEL}
_SPIN = "|/-\\"


def wants_guided(args: argparse.Namespace) -> bool:
    """A terminal start without `--bottle` or `--headless`."""
    return (not args.bottle and not args.headless
            and sys.stdin.isatty() and sys.stdout.isatty())


def logged_in_harnesses(agents: list[str]) -> list[str]:
    """The harnesses `setup` would find logged in here that have an agent."""
    # pylint: disable-next=import-outside-toplevel
    from ...agent_provider import get_provider
    ready = []
    for name in HARNESSES:
        if name not in agents:
            continue
        detection = get_provider(name).detect_host(os.environ)
        if detection.logged_in or (name == "pi" and detection.installed):
            ready.append(name)
    return ready


def pick_agent(named: str | None, manifest: "ManifestIndex", *,
               interactive: bool = True) -> str | None:
    """The agent to run: the one named (remembered from now on), the one
    remembered, else a pick (only on a terminal; without one the only
    logged-in harness, or nothing). None when there's nothing to run or it's
    cancelled."""
    agents = manifest.all_agent_names
    if named:
        manifest.require_agent(named)
        start_prefs.remember_agent(named)
        return named
    remembered = start_prefs.remembered_agent()
    if remembered in agents:
        return remembered
    choices = logged_in_harnesses(agents) or [a for a in agents if a != "bottle-architect"]
    if not choices or (len(choices) > 1 and not interactive):
        return None
    picked = (choices[0] if len(choices) == 1
              else tui.filter_select(choices, title="Run which agent?"))
    if picked:
        start_prefs.remember_agent(picked)
    return picked


def ask_task() -> str:
    sys.stderr.write("What should the agent do? (Enter to just open it)\n> ")
    sys.stderr.flush()
    return read_tty_line().strip()


def suggest_with_spinner(task: str, cwd: Path, settings: Settings) -> "Proposal":
    """The suggestion, with a spinner on the terminal while it runs."""
    done = threading.Event()

    def spin() -> None:
        i = 0
        while not done.wait(0.1):
            sys.stderr.write(f"\r{_SPIN[i % len(_SPIN)]} suggesting a bottle…")
            sys.stderr.flush()
            i += 1
        sys.stderr.write("\r" + " " * 30 + "\r")
        sys.stderr.flush()

    spinner = threading.Thread(target=spin, daemon=True)
    spinner.start()
    try:
        return suggest_client.suggest(task, cwd, settings=settings)
    finally:
        done.set()
        spinner.join()


def edit_proposal(proposal: "Proposal", dirs: list[tuple[Path, bool]]) -> "Proposal | None":
    """The proposal as edited in `$EDITOR`, reopened with the errors inline
    until it validates; None when the file is emptied (cancel)."""
    # pylint: disable=import-outside-toplevel
    from ...contract.yaml_subset import YamlSubsetError, parse_frontmatter, serialize_yaml_subset
    from ...suggest.proposal import Proposal as ProposalType
    from ...suggest.validate import validate_proposal
    doc = {k: v for k, v in proposal.to_dict().items() if k != "source"}
    text, errors = serialize_yaml_subset(doc), []
    while True:
        header = ["# Edit the suggested bottle; save and quit to use it. Empty the file to cancel."]
        header += [f"# error: {e}" for e in errors]
        edited = run_editor("\n".join(header) + "\n" + text)
        body = "\n".join(line for line in edited.splitlines() if not line.startswith("#"))
        if not body.strip():
            return None
        text = body + "\n"
        try:
            raw = parse_frontmatter(f"---\n{text}---\n")[0]
            candidate = ProposalType.from_dict(raw)
        except (YamlSubsetError, ValueError) as e:
            errors = [str(e)]
            continue
        validated = validate_proposal(candidate, dirs)[0]
        errors = [n for n in validated.notes
                  if n.startswith("dropped ") and n not in candidate.notes]
        if not errors:
            validated.source = proposal.source
            return validated


def run_editor(text: str) -> str:
    """`text` as the user left it in `$VISUAL`/`$EDITOR` (default `vi`)."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", prefix="bottle-", delete=False,
                                     encoding="utf-8") as f:
        f.write(text)
    try:
        subprocess.run([*shlex.split(editor), f.name], check=False)
        return Path(f.name).read_text(encoding="utf-8")
    finally:
        os.unlink(f.name)


def ask_bottle_name() -> str:
    sys.stderr.write("Save as (a bottle name, e.g. weather-cli): ")
    sys.stderr.flush()
    return read_tty_line().strip()


def bottles_for(proposal: "Proposal", *, saved_as: str = "") -> tuple[tuple[str, ...], str]:
    """The launch's bottles and its run bottle's text ("" when it needs
    none): the proposal's bottles plus its additions, saved as `saved_as` or
    unsaved as this run's own bottle."""
    # pylint: disable=import-outside-toplevel
    from ...suggest.validate import is_stacked, stack_names
    from ...suggest.write import bottle_text
    if is_stacked(proposal) and proposal.additions.is_empty():
        return tuple(proposal.bottles) or (DEFAULT_BOTTLE,), ""
    if saved_as:
        return tuple(stack_names(proposal, saved_as)), ""
    return tuple(stack_names(proposal, RUN_BOTTLE)), bottle_text(proposal)


def ask_choice(*, blocked: bool = False) -> str:
    """Step 4's choice, one keypress on a line under the proposal (no full
    screen picker, so the proposal stays readable). A blocked proposal
    offers only edit and cancel."""
    # pylint: disable-next=import-outside-toplevel
    from .supervise import read_key
    sys.stdout.write(BLOCKED_PROMPT if blocked else CONFIRM_PROMPT)
    sys.stdout.flush()
    while True:
        choice = _KEYS.get(read_key())
        if choice is not None and not (blocked and choice in (RUN, SAVE)):
            sys.stdout.write("\n")
            return choice


def confirm(proposal: "Proposal", dirs: list[tuple[Path, bool]]
            ) -> tuple["Proposal", str] | None:
    """Step 4: (the proposal to run, the name it was saved as or ""), or None
    when cancelled."""
    # pylint: disable=import-outside-toplevel
    from ...suggest.render import render_proposal
    from ...suggest.write import SuggestWriteError, write_bottle
    while True:
        choice = ask_choice(blocked=bool(proposal.blocked))
        if choice == RUN:
            return proposal, ""
        if choice == SAVE:
            name = ask_bottle_name()
            try:
                path = write_bottle(name, proposal)
            except SuggestWriteError as e:
                sys.stderr.write(f"bot-bottle: {e}\n")
                continue
            info(f"saved {path}")
            return proposal, name
        if choice == EDIT:
            edited = edit_proposal(proposal, dirs)
            if edited is not None:
                proposal = edited
                print(render_proposal(proposal, dirs, compact=True))
            continue
        return None


def guided_start(args: argparse.Namespace, manifest: "ManifestIndex", settings: Settings, *,
                 launch, auto: bool = False) -> int:  # type: ignore[no-untyped-def]
    """Steps 1-5. `launch(request, index)` starts it; `auto` skips step 4."""
    # pylint: disable=import-outside-toplevel
    from ...paths import bot_bottle_root
    from ...run_bottle import index_for
    from ...suggest.flow import bottle_dirs
    from ...suggest.render import render_proposal
    # The architect's bottle boots while you choose and type.
    threading.Thread(target=suggest_client.warm, args=(settings,), daemon=True).start()
    interactive = sys.stdin.isatty()
    agent = pick_agent(args.name, manifest, interactive=interactive)
    if agent is None:
        sys.stderr.write("bot-bottle: which agent? name one (`bot-bottle start <agent>`), or "
                         "run `bot-bottle setup` for the starters\n")
        return 1
    task = args.prompt if args.prompt is not None else ask_task()
    cwd = Path(os.getcwd())
    base = LaunchRequest(agent=agent, bottles=(DEFAULT_BOTTLE,), label=args.label or agent,
                         color=args.color or "", image_policy="prefer-cached",
                         settings=settings, cwd=str(cwd), copy_cwd=args.cwd)
    if not task:
        return launch(base, manifest)
    proposal = suggest_with_spinner(task, cwd, settings)
    dirs = bottle_dirs(cwd, bot_bottle_root())
    saved_as = ""
    if proposal.edits:
        sys.stderr.write(f"bot-bottle: {skipped_edits(proposal)}\n")
        proposal = without_edits(proposal, dirs)
    if auto and proposal.blocked:
        sys.stderr.write(f"bot-bottle: --auto: the suggested bottle can't run here: "
                         f"{proposal.blocked}\n")
        return 1
    if auto:
        print(auto_line(proposal), file=sys.stderr)
    else:
        print(render_proposal(proposal, dirs, compact=True))
        confirmed = confirm(proposal, dirs)
        if confirmed is None:
            info("cancelled")
            return 0
        proposal, saved_as = confirmed
    bottles, run_text = bottles_for(proposal, saved_as=saved_as)
    request = dataclasses.replace(base, bottles=bottles, prompt=task, run_bottle=run_text)
    return launch(request, index_for(manifest, text=run_text))


def skipped_edits(proposal: "Proposal") -> str:
    """What's said when a proposal's edits to other bottles are skipped
    (only the guided view asks about each)."""
    names = ", ".join(e.bottle for e in proposal.edits)
    return (f"skipped the suggested edit{'s' if len(proposal.edits) > 1 else ''} to {names}; "
            f"`bot-bottle start` on a terminal reviews them")


def without_edits(proposal: "Proposal", dirs: list[tuple[Path, bool]]) -> "Proposal":
    """`proposal` without its edits, validated again (its stack may have
    named a bottle one of them made)."""
    # pylint: disable-next=import-outside-toplevel
    from ...suggest.validate import validate_proposal
    again = validate_proposal(dataclasses.replace(proposal, edits=[]), dirs)[0]
    again.source = proposal.source
    return again


def auto_line(proposal: "Proposal") -> str:
    """`--auto`'s one line: the bottles it stacks and what it adds."""
    add = proposal.additions
    added = ([f"egress {e.host}" for e in add.egress] + [f"repo {r.grant}" for r in add.repos]
             + [f"env {v.name}" for v in add.env])
    base = " + ".join(proposal.bottles) or (proposal.new_bottle.extends if proposal.new_bottle
                                            else "") or DEFAULT_BOTTLE
    extra = f", adding {', '.join(added)}" if added else ""
    return f"bot-bottle: --auto: running in {base}{extra} (from {proposal.source or 'rules'})"


__all__ = ["auto_line", "bottles_for", "confirm", "edit_proposal", "guided_start",
           "logged_in_harnesses", "pick_agent", "wants_guided"]
