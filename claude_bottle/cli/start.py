"""start: boot a sandboxed container for a named agent and attach an
interactive claude-code session. The container is torn down when the
session ends.

The launch core is shared with `cli.py resume <identity>` and (PRD
0020 chunk 1+) the dashboard's in-process start flow: see the
public helpers `prepare_with_preflight`, `attach_claude`, and the
private orchestrator `_launch_bottle`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Callable

from ..backend import Bottle, BottleSpec, get_bottle_backend
from ..backend.docker.bottle_plan import DockerBottlePlan
from ..backend.docker.bottle_state import (
    cleanup_state,
    is_preserved,
    mark_preserved,
)
from ..backend.docker.capability_apply import snapshot_transcript
from ..log import info
from ..manifest import Manifest
from ._common import PROG, USER_CWD, read_tty_line


def cmd_start(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} start", add_help=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cwd", action="store_true", help="copy host cwd into a derived image")
    parser.add_argument("--remote-control", action="store_true")
    parser.add_argument("name", help="agent name defined in claude-bottle.json")
    args = parser.parse_args(argv)

    dry_run = args.dry_run or os.environ.get("CLAUDE_BOTTLE_DRY_RUN") == "1"

    manifest = Manifest.resolve(USER_CWD)
    spec = BottleSpec(
        manifest=manifest,
        agent_name=args.name,
        copy_cwd=args.cwd,
        user_cwd=USER_CWD,
    )
    return _launch_bottle(
        spec,
        dry_run=dry_run,
        remote_control=args.remote_control,
    )


# --- Public helpers shared with the dashboard (PRD 0020) -----------------


def prepare_with_preflight(
    spec: BottleSpec,
    *,
    stage_dir: Path,
    render_preflight: Callable[[DockerBottlePlan], None],
    prompt_yes: Callable[[], bool],
    dry_run: bool = False,
) -> tuple[DockerBottlePlan | None, str]:
    """Run `backend.prepare`, render the preflight summary via the
    injected callable, prompt y/N via the injected callable. The CLI
    binds these to stderr/stdin; the dashboard binds them to a
    curses modal.

    Returns `(plan, identity)`. `plan` is None on dry-run or
    operator-N, but `identity` is set as soon as `backend.prepare`
    returns so callers can reap the prepare-time state dir via
    `settle_state(identity)` in their finally — exactly the existing
    semantics."""
    backend = get_bottle_backend()
    plan = backend.prepare(spec, stage_dir=stage_dir)
    identity = _identity_from_plan(plan)

    render_preflight(plan)

    if dry_run:
        info("dry-run requested; not starting container.")
        return None, identity
    if not prompt_yes():
        info("aborted by user")
        return None, identity
    return plan, identity


def attach_claude(
    bottle: Bottle, *, remote_control: bool = False,
) -> int:
    """Run claude inside `bottle` as an interactive session. Blocks
    until the session ends; returns the claude process's exit code.

    Used as the inner step of `./cli.py start` (one-shot) and by the
    dashboard (PRD 0020), which calls it from inside a `curses.endwin
    → … → stdscr.refresh()` handoff so the curses surface gets out
    of the terminal's way while claude has it."""
    info(
        "attaching interactive claude session "
        "(Ctrl-D or 'exit' to leave; container will be removed)"
    )
    claude_args = ["--dangerously-skip-permissions"]
    if remote_control:
        claude_args.append("--remote-control")
    return bottle.exec_claude(claude_args, tty=True)


def capture_session_state(identity: str, exit_code: int) -> None:
    """Inside the launch context, while the container is still
    alive: snapshot the transcript and mark for preservation if
    claude crashed. Public for the dashboard's death-handling path
    (PRD 0020 open question 3)."""
    if not identity:
        return
    snapshot_transcript(identity)
    if exit_code != 0:
        mark_preserved(identity)


def settle_state(identity: str) -> None:
    """Post-teardown housekeeping: print the resume hint if the
    state was preserved, otherwise reap the per-bottle state dir.
    Public so the dashboard's explicit-stop path calls the same
    settlement the CLI uses on context exit."""
    if not identity:
        return
    if is_preserved(identity):
        info(f"to resume this bottle: ./cli.py resume {identity}")
        return
    cleanup_state(identity)


def _identity_from_plan(plan: object) -> str:
    """Backend-specific: the docker plan exposes the identity as
    `.slug`. Other backends in the future would expose their own
    identity attribute; for now we duck-type to keep this layer
    backend-agnostic."""
    return getattr(plan, "slug", "")


def _text_prompt_yes() -> bool:
    """Default `prompt_yes` for CLI use: reads y/N from the
    controlling tty via stderr prompt + tty-line read."""
    sys.stderr.write("claude-bottle: launch this agent? [y/N] ")
    sys.stderr.flush()
    reply = read_tty_line()
    return reply in ("y", "Y", "yes", "YES")


def _text_render_preflight(*, remote_control: bool):
    def _render(plan: DockerBottlePlan) -> None:
        plan.print(remote_control=remote_control)
    return _render


def _launch_bottle(
    spec: BottleSpec,
    *,
    dry_run: bool,
    remote_control: bool,
) -> int:
    """Shared launch core for `start` and `resume`. Builds the plan,
    prints / dry-runs / prompts as appropriate, brings the bottle up,
    attaches claude, and prints the resume hint on session end."""
    stage_dir = Path(tempfile.mkdtemp(prefix="claude-bottle-stage."))
    identity = ""
    try:
        plan, identity = prepare_with_preflight(
            spec,
            stage_dir=stage_dir,
            render_preflight=_text_render_preflight(remote_control=remote_control),
            prompt_yes=_text_prompt_yes,
            dry_run=dry_run,
        )
        if plan is None:
            return 0

        backend = get_bottle_backend()
        with backend.launch(plan) as bottle:
            exit_code = attach_claude(bottle, remote_control=remote_control)
            info(
                f"session ended (exit {exit_code}); "
                f"container {bottle.name} will be removed"
            )
            # While the container is still alive: always snapshot the
            # transcript and — if the agent exited non-zero — mark
            # the state for preservation. Capability-block already
            # did both before triggering teardown from the dashboard;
            # this picks up crashes / Ctrl-Cs / OOM kills the same
            # way. snapshot_transcript is best-effort so the
            # capability-block path's prior snapshot isn't clobbered
            # when the container is already gone.
            capture_session_state(identity, exit_code)
        return 0
    finally:
        # PRD 0018 chunk 2: prepare now writes the bottle's bind-mount
        # sources under state/<slug>/. If we never reached the
        # launch context (dry-run, preflight-N, prepare exception), or
        # we did but nothing requested preservation, reap them along
        # with everything else. `settle_state` subsumes the prior
        # post-launch settlement and the new pre-launch cleanup.
        settle_state(identity)
        shutil.rmtree(stage_dir, ignore_errors=True)
