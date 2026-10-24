"""`bot-bottle` with no command: start an agent on this directory.

  bot-bottle             the agent last used here, else the default
                         (`bot-bottle default <agent>`), else the first ready
                         starter: claude, codex, then pi
  bot-bottle <agent>     that agent
  bot-bottle default <agent>
                         set the default agent

The bottle is the agent's own, plus the project's bottle when the project
ships one (`./.bot-bottle/bottles/<directory name>.md`, trusted on this host
first) or else the built-in `basic` (package registries and public code,
read-only). This directory is copied into the bottle (not your home
directory or `/`; a git repo filtered by its .gitignore), and when the agent
exits its changes are saved as a patch to apply here. The bottle never writes
to your files: a mount would let the agent plant code the host then runs, such
as a `.git/config` that git executes. With no starter yet, `bot-bottle setup` runs
first. `bot-bottle start` keeps the pickers.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ...contract.hostctl import LaunchRequest
from ...lib.log import die, info
from ...manifest import ManifestIndex
from ...paths import bot_bottle_root
from ...util import slugify
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
    """The agent for bare `bot-bottle` in `cwd`: last used here, else the
    default, else the first starter."""
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
    """With no agent to pick, run `setup` in a terminal and look again."""
    if not sys.stdin.isatty():
        return manifest
    info("no agent yet; running setup first")
    from .setup import cmd_setup  # pylint: disable=import-outside-toplevel
    cmd_setup(["--no-pair"])
    return ManifestIndex.resolve(str(cwd))


def project_bottle(cwd: Path) -> str:
    """The project's own bottle, named after its directory, if any."""
    name = slugify(cwd.name)
    return name if (cwd / ".bot-bottle" / "bottles" / f"{name}.md").is_file() else ""


def cmd_quick(argv: list[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    names = [a for a in argv if a not in ("-v", "--verbose")]
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
    remember(cwd, agent)
    settings = settings_from_env()
    request = LaunchRequest(
        agent=agent,
        bottles=(bottle, project_bottle(cwd) or "basic"),
        label=_uniquify_label(settings, agent),
        image_policy="prefer-cached",
        assume_yes=True,
        settings=settings,
        cwd=str(cwd),
        copy_cwd=share,
    )
    dry_run = os.environ.get("BOT_BOTTLE_DRY_RUN") == "1"
    return _launch_bottle(request, manifest, dry_run=dry_run, verbose=verbose)
