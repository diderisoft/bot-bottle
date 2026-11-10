"""`bot-bottle` with no command: start an agent on this directory.

  bot-bottle             the agent last used here, else the default
                         (`bot-bottle agents default <agent>`), else the first ready
                         starter: claude, codex, then pi
  bot-bottle <agent>     that agent
  bot-bottle agents default <agent>
                         set the default agent

The bottle is the project's when the project ships one (`./main`, from
`./.bot-bottle/bottles/main.md`, trusted on this host first), or else the
built-in `basic` (package registries and public code, read-only). Agents don't
name a bottle; `bot-bottle start` composes others. This directory is copied
into the bottle (not your home directory or `/`; a git repo filtered by its
.gitignore), and when the agent
exits its changes are saved as a patch to apply here. The bottle never writes
to your files: a mount would let the agent plant code the host then runs, such
as a `.git/config` that git executes. With no starter yet, `bot-bottle setup` runs
first. `bot-bottle start` keeps the pickers.

In a project with no bottle yet, a launch from a terminal first drafts one from
the checkout and, once you accept it, writes it as `./main` for the repo to
share: its `origin`, read-only unless you make it writable, when a bottle of
yours declares that forge (`bot-bottle forge connect`). `--no-onboard` skips
the draft and uses `basic`. `--dry-run` prints the preflight and starts
nothing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ...contract.hostctl import LaunchRequest
from ...lib.log import die, info
from ...manifest import ManifestIndex
from ...manifest.loader import PROJECT_BOTTLE, bottle_ref
from ...manifest.schema import DEFAULT_BOTTLE
from ...util import read_tty_line
from ..constants import PROG
from ..host_state_client import agent_prefs, remember_agent
from ..settings import settings_from_env
from .start import _launch_bottle

STARTERS = ("claude", "codex", "pi")


def pick_agent(manifest: ManifestIndex, cwd: Path) -> str | None:
    """The agent for bare `bot-bottle` in `cwd`: last used here, else the
    remembered one, else the first starter."""
    prefs = agent_prefs()
    for name in (prefs.dirs.get(str(cwd)), prefs.default, *STARTERS):
        if isinstance(name, str) and name in manifest.all_agent_names:
            return name
    return None


def remember(cwd: Path, agent: str) -> None:
    remember_agent(agent, str(cwd))


def default_agent() -> str:
    """The agent a bare `bot-bottle` starts where none was used last, or ""."""
    return agent_prefs().default


def cmd_default(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        default = default_agent()
        sys.stderr.write(f"usage: {PROG} agents default <agent>\n")
        if default:
            sys.stderr.write(f"current default: {default}\n")
        return 0 if argv else 2
    agent = argv[0]
    if agent not in ManifestIndex.resolve(os.getcwd()).all_agent_names:
        die(f"no agent named {agent!r}; `{PROG} agents` lists them")
    remember_agent(agent)
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
    """The project's own bottle, `./main`, if it has one."""
    path = cwd / ".bot-bottle" / "bottles" / f"{PROJECT_BOTTLE}.md"
    return bottle_ref(PROJECT_BOTTLE, True) if path.is_file() else ""


def _ask(question: str) -> str:
    sys.stderr.write(question)
    sys.stderr.flush()
    return read_tty_line()


def _say(line: str) -> None:
    sys.stderr.write(line + "\n")


def _connect(url: str) -> bool:
    """`forge connect <url>`, for onboarding; whether it connected."""
    from .forge import connect  # pylint: disable=import-outside-toplevel
    try:
        return connect([url]) == 0
    except SystemExit:
        return False


def _onboard(cwd: Path) -> str:
    """Draft and write the project's bottle; "" when the user skips."""
    from ...onboarding import onboard  # pylint: disable=import-outside-toplevel
    return onboard(cwd, ask=_ask, say=_say, connect=_connect) or ""


def cmd_quick(argv: list[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    no_onboard = "--no-onboard" in argv
    dry_run = "--dry-run" in argv
    names = [a for a in argv if a not in ("-v", "--verbose", "--no-onboard", "--dry-run")]
    cwd = Path.cwd()
    manifest = ManifestIndex.resolve(str(cwd))
    if names:
        agent: str | None = names[0]
        if names[0] not in manifest.all_agent_names:
            die(f"unknown command or agent: {names[0]} (`{PROG} help` lists commands, "
                f"`{PROG} agents` lists agents)")
    else:
        agent = pick_agent(manifest, cwd)
        if agent is None:
            manifest = _ensure_starters(manifest, cwd)
            agent = pick_agent(manifest, cwd)
        if agent is None:
            die(f"no agent to start; run `{PROG} setup`, or add one under ~/.bot-bottle/agents/")
    share = cwd not in (Path.home(), Path("/"))
    if not share:
        info(f"not sharing {cwd} with the bottle; cd into a project to work on it")
    remember(cwd, agent)
    project = project_bottle(cwd)
    if not project and share and not (no_onboard or dry_run) and sys.stdin.isatty():
        project = _onboard(cwd)
    settings = settings_from_env()
    request = LaunchRequest(
        agent=agent,
        bottles=(project or DEFAULT_BOTTLE,),
        label=agent,
        image_policy="prefer-cached",
        assume_yes=True,
        settings=settings,
        cwd=str(cwd),
        copy_cwd=share,
    )
    return _launch_bottle(request, manifest, dry_run=dry_run, verbose=verbose)
