"""`bot-bottle` with no command: start an agent on this directory (PRD 0094).

  bot-bottle             the agent last used here, else your default
                         (`bot-bottle default <agent>`), else the first ready
                         starter: claude, codex, then pi
  bot-bottle <agent>     that agent
  bot-bottle --copy      copy this directory in instead of mounting it
  bot-bottle default <agent>
                         set the agent bare `bot-bottle` falls back to

The agent's own bottle is used. This directory is mounted into the bottle, so
the agent edits your files in place (not your home directory or `/`). With
--copy, or on Firecracker, which can't mount, it's copied in instead (a git
repo filtered by its .gitignore), and when the agent exits its changes are
saved as a patch to apply here. With no starter yet, `bot-bottle setup` runs
first. `bot-bottle start` keeps the pickers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ...contract.hostctl import LaunchRequest
from ...lib.log import die, info
from ...manifest import ManifestIndex
from ...paths import bot_bottle_root
from ..constants import PROG
from ..settings import settings_from_env
from .start import _launch_bottle, _peek_agent_bottle, _uniquify_label

STARTERS = ("claude", "codex", "pi")


def _state_path() -> Path:
    return bot_bottle_root() / "state" / "quickstart.json"


def _load_state() -> dict[str, object]:
    try:
        raw = json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_state(state: dict[str, object]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def pick_agent(manifest: ManifestIndex, cwd: Path) -> str | None:
    """The agent for bare `bot-bottle` here: last used in ``cwd``, else the
    default, else the first starter that exists."""
    state = _load_state()
    last = state.get("last")
    candidates = [
        last.get(str(cwd)) if isinstance(last, dict) else None,
        state.get("default"),
        *STARTERS,
    ]
    for name in candidates:
        if isinstance(name, str) and name in manifest.all_agent_names:
            return name
    return None


def remember(cwd: Path, agent: str) -> None:
    state = _load_state()
    last = state.get("last")
    last = dict(last) if isinstance(last, dict) else {}
    last[str(cwd)] = agent
    state["last"] = last
    _save_state(state)


def cmd_default(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        default = _load_state().get("default")
        sys.stderr.write(f"usage: {PROG} default <agent>\n")
        if isinstance(default, str):
            sys.stderr.write(f"current default: {default}\n")
        return 0 if argv else 2
    agent = argv[0]
    if agent not in ManifestIndex.resolve(os.getcwd()).all_agent_names:
        die(f"no agent named {agent!r}; `{PROG} list` shows them")
    state = _load_state()
    state["default"] = agent
    _save_state(state)
    info(f"`{PROG}` now starts {agent} where no other agent was used last")
    return 0


def _ensure_starters(manifest: ManifestIndex, cwd: Path) -> ManifestIndex:
    """With no agent to pick, run `setup` in a terminal, then look again."""
    if not sys.stdin.isatty():
        return manifest
    info("no agent yet; running setup first")
    from .setup import cmd_setup  # pylint: disable=import-outside-toplevel
    cmd_setup(["--no-pair"])
    return ManifestIndex.resolve(str(cwd))


def _uncommitted(cwd: Path) -> int:
    """How many files have uncommitted changes in a git repo (0 elsewhere)."""
    try:
        out = subprocess.run(["git", "-C", str(cwd), "status", "--porcelain"],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return 0
    return len(out.splitlines())


def cmd_quick(argv: list[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    copy_mode = "--copy" in argv
    names = [a for a in argv if a not in ("-v", "--verbose", "--copy")]
    cwd = Path.cwd()
    manifest = ManifestIndex.resolve(str(cwd))
    if names:
        agent: str | None = names[0]
        if names[0] not in manifest.all_agent_names:
            die(f"unknown command or agent: {names[0]} (`{PROG} help` lists commands, "
                f"`{PROG} list` lists agents)")
    else:
        agent = pick_agent(manifest, cwd)
        if agent is None:
            manifest = _ensure_starters(manifest, cwd)
            agent = pick_agent(manifest, cwd)
        if agent is None:
            die(f"no agent to start; run `{PROG} setup`, or add one under ~/.bot-bottle/agents/")
    bottle = _peek_agent_bottle(manifest, agent)
    if not bottle:
        die(f"agent {agent} doesn't name a bottle; run `{PROG} start --pick {agent}`")
    share = cwd not in (Path.home(), Path("/"))
    if not share:
        info(f"not sharing {cwd} with the bottle; cd into a project to work on it")
    elif not copy_mode and (changed := _uncommitted(cwd)):
        info(f"{changed} file{'s' if changed != 1 else ''} here have uncommitted changes; "
             "the agent edits these files directly (--copy to give it a copy)")
    remember(cwd, agent)
    settings = settings_from_env()
    request = LaunchRequest(
        agent=agent,
        bottles=(bottle,),
        label=_uniquify_label(settings, agent),
        image_policy="prefer-cached",
        assume_yes=True,
        settings=settings,
        cwd=str(cwd),
        copy_cwd=share and copy_mode,
        mount_cwd=share and not copy_mode,
    )
    dry_run = os.environ.get("BOT_BOTTLE_DRY_RUN") == "1"
    return _launch_bottle(request, manifest, dry_run=dry_run, verbose=verbose)
