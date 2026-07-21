"""start: boot a sandboxed container for a named agent and attach an
interactive claude-code session. The container is torn down when the
session ends.

`--headless` selects a non-interactive launch (agent/bottles/label from
flags, no TUI selectors, no y/N prompt) for orchestrators,
CI, and webhook dispatch. The agent still execs on the inherited
stdio/PTY, so an orchestrator that allocates the PTY drives the session.

The launch core is shared with `cli.py resume <identity>` through
the private orchestrator `_launch_bottle`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Callable

from ..agent_provider import get_provider, runtime_for
from ..backend import (
    Bottle,
    BottleSpec,
    enumerate_active_agents,
    get_bottle_backend,
)
from ..backend.docker import util as docker_mod
from ..backend.docker.bottle_plan import DockerBottlePlan
from ..bottle_state import (
    cleanup_state,
    is_preserved,
    mark_preserved,
)
from ..image_cache import StaleImageError
from ..log import info, die
from ..manifest import Manifest, ManifestIndex
from ._common import PROG, USER_CWD, read_tty_line
from . import tui


def cmd_start(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} start", add_help=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cwd", action="store_true", help="copy host cwd into the running bottle")
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help=(
            "rebuild agent/sidecar images from scratch, bypassing the "
            "build layer cache. Use when an image looks broken after a "
            "dependency bump — e.g. an installer's optionalDependencies "
            "fetch silently no-op'd on a transient failure and got baked "
            "into a cached layer."
        ),
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help=(
            "non-interactive launch: take agent/bottles/label from flags, "
            "skip all prompts. For orchestrators, CI, and webhooks."
        ),
    )
    parser.add_argument(
        "--cached-images",
        action="store_true",
        help=(
            "quickstart with existing local agent and sidecar images; "
            "only valid with --headless"
        ),
    )
    parser.add_argument(
        "--bottle",
        action="append",
        default=None,
        metavar="NAME",
        help=(
            "bottle to compose, repeatable (order = merge order). In "
            "--headless, defaults to the agent's own bottle when omitted."
        ),
    )
    parser.add_argument(
        "--label",
        default=None,
        help="bottle label / terminal title (--headless default: agent name)",
    )
    parser.add_argument(
        "--color",
        default=None,
        help="bottle color, one of the 16 ANSI color names (--headless default: none)",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="initial task prompt delivered to the agent (required with --headless)",
    )
    parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help="agent name defined in bot-bottle.json (omit to pick interactively)",
    )
    args = parser.parse_args(argv)
    if args.cached_images and not args.headless:
        die("--cached-images is only supported with --headless")

    dry_run = args.dry_run or os.environ.get("BOT_BOTTLE_DRY_RUN") == "1"
    if args.no_cache or os.environ.get("BOT_BOTTLE_NO_CACHE") == "1":
        # Read by build_image() in each backend's util module — set here
        # so both the interactive and --headless paths pick it up without
        # threading a no_cache field through every backend's plan dataclass.
        os.environ["BOT_BOTTLE_NO_CACHE"] = "1"

    manifest = ManifestIndex.resolve(USER_CWD)

    if args.headless:
        return _start_headless(
            manifest, args, dry_run=dry_run
        )

    agent_name: str | None = args.name
    if agent_name is None:
        if not manifest.all_agent_names:
            print(
                "bot-bottle: no agents defined. "
                "Add an agent to ~/.bot-bottle/agents/ or ./bot-bottle/agents/ to get started.",
                file=sys.stderr,
            )
            return 1
        agent_name = tui.filter_select(
            manifest.all_agent_names,
            title="Select agent",
        )
        if agent_name is None:
            return 0

    # Bottle multiselect: always show after agent selection so operators
    # can compose bottles at launch time without editing agent manifests.
    available_bottles = manifest.all_bottle_names
    lineage_map = _bottle_lineage(manifest)
    display_labels = [lineage_map.get(n, n) for n in available_bottles]
    label_to_name = {lineage_map.get(n, n): n for n in available_bottles}
    initial_bottle = _peek_agent_bottle(manifest, agent_name)
    initial_labels = [lineage_map.get(initial_bottle, initial_bottle)] if initial_bottle else []
    selected_labels = tui.filter_multiselect(
        display_labels,
        title="Select bottles",
        initial=initial_labels,
    )
    if selected_labels is None:
        return 0
    bottle_names = tuple(label_to_name.get(lbl, lbl) for lbl in selected_labels)

    label, color = tui.name_color_modal(default_label=agent_name)
    label, color = _resolve_unique_label(label, color)

    image_policy = _select_image_policy()
    if image_policy is None:
        return 0

    spec = BottleSpec(
        manifest=manifest,
        agent_name=agent_name,
        copy_cwd=args.cwd,
        user_cwd=USER_CWD,
        label=label,
        color=color,
        bottle_names=bottle_names,
        image_policy=image_policy,
    )
    return _launch_bottle(
        spec,
        dry_run=dry_run,
    )


# --- Headless launch -----------------------------------------------------


def _start_headless(
    manifest: ManifestIndex,
    args: argparse.Namespace,
    *,
    dry_run: bool,
) -> int:
    """Non-interactive launch path for orchestrators / CI / webhooks.

    Resolves agent, bottles, label, and color from flags + manifest
    defaults instead of the TUI selectors, and auto-confirms the
    preflight. Otherwise runs the same launch core as the interactive
    path, so the agent still execs on the inherited stdio/PTY — an
    orchestrator allocates that PTY and relays it to its
    desktop/mobile clients."""
    agent_name = args.name
    if not agent_name:
        die("--headless requires an agent name: ./cli.py start <agent> --headless")
    manifest.require_agent(agent_name)  # raises ManifestError if unknown

    prompt = args.prompt
    if not prompt:
        die(
            "--headless requires --prompt: "
            "./cli.py start <agent> --headless --prompt 'Do the thing'"
        )

    if args.bottle:
        bottle_names: tuple[str, ...] = tuple(args.bottle)
    else:
        default_bottle = _peek_agent_bottle(manifest, agent_name)
        if not default_bottle:
            die(
                f"--headless: agent '{agent_name}' has no default bottle; "
                f"pass one or more --bottle NAME"
            )
        bottle_names = (default_bottle,)

    label = _uniquify_label_headless(args.label or agent_name)

    spec = BottleSpec(
        manifest=manifest,
        agent_name=agent_name,
        copy_cwd=args.cwd,
        user_cwd=USER_CWD,
        label=label,
        color=args.color or "",
        bottle_names=bottle_names,
        headless=True,
        image_policy="cached" if args.cached_images else "fresh",
    )
    return _launch_bottle(
        spec,
        dry_run=dry_run,
        assume_yes=True,
        headless_prompt_text=prompt,
    )


def _uniquify_label_headless(label: str) -> str:
    """Non-interactive analog of `_resolve_unique_label`: if the label's
    slug collides with a running bottle, append -2, -3, … until free,
    logging the chosen label. Orchestrators fire-and-forget many bottles,
    so silently picking a free name beats erroring on every collision."""
    active_slugs = {a.slug for a in enumerate_active_agents()}
    if docker_mod.slugify(label) not in active_slugs:
        return label
    n = 2
    while docker_mod.slugify(f"{label}-{n}") in active_slugs:
        n += 1
    chosen = f"{label}-{n}"
    info(f"label '{label}' already in use; using '{chosen}'")
    return chosen


# --- Launch helpers ------------------------------------------------------


def prepare_with_preflight(
    spec: BottleSpec,
    *,
    stage_dir: Path,
    render_preflight: Callable[[DockerBottlePlan, str], None],
    prompt_yes: Callable[[], bool],
    dry_run: bool = False,
    backend_name: str | None = None,
) -> tuple[DockerBottlePlan | None, str]:
    """Run `backend.prepare`, render the preflight summary via the
    injected callable, prompt y/N via the injected callable.

    `backend_name` selects which backend prepares the plan
    (`None` → `$BOT_BOTTLE_BACKEND` → host auto-selection).

    When `spec.headless` is True the docker-fallback prompt is suppressed:
    auto-selection dies with an actionable message rather than blocking
    on a TTY read (which would hang CI, webhook dispatch, and orchestrators).

    Returns `(plan, identity)`. `plan` is None on dry-run or
    operator-N, but `identity` is set as soon as `backend.prepare`
    returns so callers can reap the prepare-time state dir via
    `settle_state(identity)` in their finally — exactly the existing
    semantics."""
    backend = get_bottle_backend(backend_name, prompt=not spec.headless)
    plan = backend.prepare(spec, stage_dir=stage_dir)
    identity = _identity_from_plan(plan)

    render_preflight(plan, backend.name)

    if dry_run:
        info("dry-run requested; not starting container.")
        return None, identity
    if not prompt_yes():
        info("aborted by user")
        return None, identity
    return plan, identity


def attach_agent(
    bottle: Bottle, *, resume: bool = False,
    agent_provider_template: str = "claude",
    startup_args: tuple[str, ...] = (),
) -> int:
    """Run the selected provider CLI inside `bottle` as an
    interactive session. Blocks until the session ends; returns the
    agent process's exit code.

    `resume=True` adds `--continue` so claude picks up its most
    recent session non-interactively (no session-picker prompt).
    First-attach paths (`./cli.py start`) leave it False.

    Used as the inner step of `./cli.py start`."""
    runtime = runtime_for(agent_provider_template)
    info(
        f"attaching interactive {agent_provider_template} session "
        "(Ctrl-D or 'exit' to leave; container will be removed)"
    )
    agent_args = list(runtime.bypass_args)
    agent_args.extend(startup_args)
    if resume:
        agent_args.extend(runtime.resume_args)
    return bottle.exec_agent(agent_args, tty=True)


def capture_claude_session_state(identity: str, exit_code: int) -> None:
    """Inside the launch context, while the container is still
    alive: snapshot the transcript and mark for preservation if
    claude crashed."""
    # FIXME: this captures Claude-specific session state. A follow-up
    # spike should explore freezing provider-neutral container state
    # instead of relying on each agent's transcript layout.
    if not identity:
        return
    # snapshot_transcript(identity)
    if exit_code != 0:
        mark_preserved(identity)


def settle_state(identity: str) -> None:
    """Post-teardown housekeeping: print the resume hint if the
    state was preserved, otherwise reap the per-bottle state dir."""
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


def _peek_agent_bottle(manifest: ManifestIndex, agent_name: str) -> str:
    """Return the `bottle:` value from the named agent's frontmatter without
    fully parsing the agent file, or "" when absent or unreadable.

    Used to pre-populate the bottle multiselect with the agent's default
    bottle so operators who haven't removed `bottle:` from their manifests
    don't need to re-select it every time."""
    if manifest.home_md is None:
        # Eager mode (from_json_obj): agent is pre-parsed.
        if agent_name in manifest.agents:
            return manifest.agents[agent_name].bottle
        return ""

    from ..manifest_loader import scan_agent_names
    from ..yaml_subset import YamlSubsetError, parse_frontmatter

    home_agents = scan_agent_names(manifest.home_md / "agents")
    cwd_agents: dict[str, Path] = {}
    if manifest.cwd_md is not None:
        cwd_agents = scan_agent_names(manifest.cwd_md / "agents")
    merged = {**home_agents, **cwd_agents}
    path = merged.get(agent_name)
    if path is None:
        return ""
    try:
        fm, _ = parse_frontmatter(path.read_text())
        bottle = fm.get("bottle", "")
        return str(bottle) if isinstance(bottle, str) else ""
    except (OSError, YamlSubsetError):
        return ""


def _resolve_unique_label(label: str, color: str) -> tuple[str, str]:
    """Re-prompt with a disclaimer until the label's slug is not already
    in use among running bottles.  Passes through unchanged when no
    collision is found on the first check."""
    while True:
        slug_candidate = docker_mod.slugify(label)
        active_slugs = {a.slug for a in enumerate_active_agents()}
        if slug_candidate not in active_slugs:
            return label, color
        label, color = tui.name_color_modal(
            default_label=label,
            disclaimer=f'"{label}" is already in use',
        )


def _text_prompt_yes() -> bool:
    """Default `prompt_yes` for CLI use: reads y/N from the
    controlling tty via stderr prompt + tty-line read."""
    sys.stderr.write("bot-bottle: launch this agent? [y/N] ")
    sys.stderr.flush()
    reply = read_tty_line()
    return reply in ("y", "Y", "yes", "YES")


def _select_image_policy() -> str | None:
    return tui.filter_select(
        ["fresh", "cached"],
        title="Select image startup mode",
    )


def _text_render_preflight():
    def _render(plan: DockerBottlePlan, backend_name: str) -> None:
        print(file=sys.stderr)
        print(f"backend: {backend_name}", file=sys.stderr)
        print(_manifest_to_yaml(plan.manifest), file=sys.stderr)
    return _render


def _bottle_lineage(manifest: ManifestIndex) -> dict[str, str]:
    """Return {bottle_name: lineage_label} for bottles that have an extends chain.

    Bottles without a parent are omitted (the caller falls back to the bare name).
    Labels show the chain root-first: e.g. 'dev -> bot-bottle-dev -> claude-dev'."""
    if manifest.home_md is None:
        return {}
    bottles_dir = manifest.home_md / "bottles"
    if not bottles_dir.is_dir():
        return {}

    from ..yaml_subset import YamlSubsetError, parse_frontmatter

    extends_of: dict[str, str] = {}
    for path in bottles_dir.glob("*.md"):
        try:
            fm, _ = parse_frontmatter(path.read_text())
            parent = fm.get("extends", "")
            if isinstance(parent, str) and parent:
                extends_of[path.stem] = parent
        except (OSError, YamlSubsetError):
            pass

    labels: dict[str, str] = {}
    for name in extends_of:
        chain = [name]
        seen = {name}
        cur = name
        while cur in extends_of:
            par = extends_of[cur]
            if par in seen:
                break
            chain.append(par)
            seen.add(par)
            cur = par
        labels[name] = " -> ".join(reversed(chain))

    return labels


def _manifest_to_yaml(manifest: Manifest) -> str:
    """Serialize the resolved Manifest to a YAML string for preflight display."""
    lines: list[str] = []

    agent = manifest.agent
    lines.append("agent:")
    if agent.skills:
        lines.append("  skills:")
        for s in agent.skills:
            lines.append(f"    - {s}")
    if not agent.git_user.is_empty():
        lines.append("  git-gate:")
        lines.append("    user:")
        if agent.git_user.name:
            lines.append(f"      name: {agent.git_user.name}")
        if agent.git_user.email:
            lines.append(f"      email: {agent.git_user.email}")

    bottle = manifest.bottle
    lines.append("bottle:")

    if bottle.agent_provider.template != "claude" or bottle.agent_provider.dockerfile:
        lines.append("  agent_provider:")
        lines.append(f"    template: {bottle.agent_provider.template}")
        if bottle.agent_provider.dockerfile:
            lines.append(f"    dockerfile: {bottle.agent_provider.dockerfile}")

    if bottle.env:
        lines.append("  env:")
        for k, v in sorted(bottle.env.items()):
            lines.append(f"    {k}: {v}")

    has_git_gate = not bottle.git_user.is_empty() or bottle.git
    if has_git_gate:
        lines.append("  git-gate:")
        if not bottle.git_user.is_empty():
            lines.append("    user:")
            if bottle.git_user.name:
                lines.append(f"      name: {bottle.git_user.name}")
            if bottle.git_user.email:
                lines.append(f"      email: {bottle.git_user.email}")
        if bottle.git:
            lines.append("    repos:")
            for entry in bottle.git:
                lines.append(f"      {entry.Name}:")
                lines.append(f"        url: {entry.Upstream}")

    if bottle.egress.routes:
        lines.append("  egress:")
        lines.append("    routes:")
        for r in bottle.egress.routes:
            lines.append(f"      - host: {r.Host}")
            if r.AuthScheme:
                lines.append(f"        auth:")
                lines.append(f"          scheme: {r.AuthScheme}")

    lines.append(f"  supervise: {'true' if bottle.supervise else 'false'}")
    if bottle.nested_containers:
        lines.append("  nested_containers: true")

    return "\n".join(lines)


def _launch_bottle(
    spec: BottleSpec,
    *,
    dry_run: bool,
    backend_name: str | None = None,
    assume_yes: bool = False,
    headless_prompt_text: str = "",
) -> int:
    """Shared launch core for `start` and `resume`. Builds the plan,
    prints / dry-runs / prompts as appropriate, brings the bottle up,
    attaches claude, and prints the resume hint on session end.

    `assume_yes` skips the interactive y/N confirmation (headless /
    orchestrator launches), where there is no human at the prompt.

    `headless_prompt_text` is passed to the provider's `headless_prompt`
    method and the resulting args are appended to startup_args so the
    agent receives the initial task without interactive input."""
    stage_dir = Path(tempfile.mkdtemp(prefix="bot-bottle-stage."))
    identity = ""
    try:
        plan, identity = prepare_with_preflight(
            spec,
            stage_dir=stage_dir,
            render_preflight=_text_render_preflight(),
            prompt_yes=(lambda: True) if assume_yes else _text_prompt_yes,
            dry_run=dry_run,
            backend_name=backend_name,
        )
        if plan is None:
            return 0

        backend = get_bottle_backend(backend_name)
        try:
            backend.prelaunch_checks(plan)
        except StaleImageError as exc:
            if assume_yes:
                die(str(exc))
            sys.stderr.write(f"bot-bottle: {exc}\nLaunch anyway? [y/N] ")
            sys.stderr.flush()
            if read_tty_line() not in ("y", "Y", "yes", "YES"):
                return 0
        with backend.launch(plan) as bottle:
            agent_provider_template = getattr(plan, "agent_provider_template", "claude")
            extra_args: tuple[str, ...] = ()
            if headless_prompt_text:
                extra_args = tuple(
                    get_provider(agent_provider_template).headless_prompt(
                        headless_prompt_text
                    )
                )
            exit_code = attach_agent(
                bottle,
                agent_provider_template=agent_provider_template,
                startup_args=plan.agent_provision.startup_args + extra_args,
            )
            info(
                f"session ended (exit {exit_code}); "
                f"container {bottle.name} will be removed"
            )
            if agent_provider_template == "claude":
                capture_claude_session_state(identity, exit_code)
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
