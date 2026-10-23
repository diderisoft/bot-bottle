"""start: launch a sandboxed bottle for a named agent and attach to its
interactive session.

The CLI resolves everything interactive (agent and bottle pickers, label
and color, secret prompts, host-key and launch confirmations), then asks
the host controller to launch. The agent runs in a tmux session the
controller owns; this terminal attaches to it. Ctrl-] detaches and leaves
the agent running (`bot-bottle attach <id>` rejoins); the bottle is torn
down when the agent exits, and `start` exits with the agent's code.

`--headless` takes agent/bottles/label from flags and skips every prompt,
for orchestrators, CI and webhooks; it still attaches on the inherited PTY.
`--dry-run` prepares locally and prints the preflight without the daemon.
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from ...backend import BottlePlan, enumerate_active_agents
from ...bottle_state import settle_state
from ...contract.hostctl import LaunchRequest, Settings
from ...lib.log import info, die
from ...manifest import ManifestIndex
from ...manifest.preview import manifest_to_yaml
from ..constants import PROG
from ..launch_client import launch, prepare_request
from ..secrets_env import secrets_from_env
from ..settings import settings_from_env
from ...util import slugify
from .. import tui


def cmd_start(argv: list[str]) -> int:  # pylint: disable=too-many-return-statements
    parser = argparse.ArgumentParser(prog=f"{PROG} start", add_help=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="stream the full launch log instead of one line per stage")
    parser.add_argument("--pick", action="store_true",
                        help="choose bottles, label, color and image mode interactively")
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
        help=("agent name from ~/.bot-bottle/agents or ./.bot-bottle/agents "
              "(omit to pick interactively)"),
    )
    args = parser.parse_args(argv)
    if args.cached_images and not args.headless:
        die("--cached-images is only supported with --headless")

    dry_run = args.dry_run or os.environ.get("BOT_BOTTLE_DRY_RUN") == "1"
    settings = settings_from_env()
    if args.no_cache:
        settings = replace(
            settings, operation=replace(settings.operation, no_cache=True))

    manifest = ManifestIndex.resolve(os.getcwd())

    if args.headless:
        return _start_headless(manifest, args, settings, dry_run=dry_run)

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

    # An agent that names its bottle starts on it with no more questions
    # (PRD 0094): its own label (deduplicated), no color, images as
    # published, no launch confirmation. `--pick` brings back the pickers to
    # compose bottles or set a label and color.
    default_bottle = _peek_agent_bottle(manifest, agent_name)
    if default_bottle and not args.pick:
        request = LaunchRequest(
            agent=agent_name,
            bottles=(default_bottle,),
            label=_uniquify_label(settings, agent_name),
            assume_yes=True,
            settings=settings,
            cwd=os.getcwd(),
            copy_cwd=args.cwd,
        )
        return _launch_bottle(request, manifest, dry_run=dry_run, verbose=args.verbose)

    # Bottle multiselect: compose bottles at launch time without editing
    # agent manifests.
    available_bottles = manifest.all_bottle_names
    lineage_map = _bottle_lineage(manifest)
    display_labels = [lineage_map.get(n, n) for n in available_bottles]
    label_to_name = {lineage_map.get(n, n): n for n in available_bottles}
    initial_labels = [lineage_map.get(default_bottle, default_bottle)] if default_bottle else []
    selected_labels = tui.filter_multiselect(
        display_labels,
        title="Select bottles",
        initial=initial_labels,
    )
    if selected_labels is None:
        return 0
    bottle_names = tuple(label_to_name.get(lbl, lbl) for lbl in selected_labels)

    # Launches are named after the agent; `--pick` asks for a label, color
    # and image mode instead.
    if args.pick:
        label, color = tui.name_color_modal(default_label=agent_name)
        label, color = _resolve_unique_label(settings, label, color)
        image_policy = _select_image_policy()
        if image_policy is None:
            return 0
    else:
        label, color, image_policy = _uniquify_label(settings, agent_name), "", "fresh"

    request = LaunchRequest(
        agent=agent_name,
        bottles=bottle_names,
        label=label,
        color=color,
        image_policy=image_policy,
        settings=settings,
        cwd=os.getcwd(),
        copy_cwd=args.cwd,
    )
    return _launch_bottle(request, manifest, dry_run=dry_run, verbose=args.verbose)


# --- Headless launch -----------------------------------------------------


def _start_headless(
    manifest: ManifestIndex,
    args: argparse.Namespace,
    settings: Settings,
    *,
    dry_run: bool,
) -> int:
    """Non-interactive launch path for orchestrators / CI / webhooks.

    Resolves agent, bottles, label, and color from flags + manifest
    defaults instead of the TUI selectors, and auto-confirms the
    preflight. The session is still attached on the inherited PTY, which
    an orchestrator allocates and relays to its clients."""
    try:
        stdin_fd = sys.stdin.fileno()
    except io.UnsupportedOperation:
        stdin_fd = -1
    if not os.isatty(stdin_fd):
        die(
            "--headless requires a PTY on stdin; run via:\n"
            "  script -q /dev/null bot-bottle start ..."
        )

    agent_name = args.name
    if not agent_name:
        die("--headless requires an agent name: bot-bottle start <agent> --headless")
    manifest.require_agent(agent_name)  # raises ManifestError if unknown

    prompt = args.prompt
    if not prompt:
        die(
            "--headless requires --prompt: "
            "bot-bottle start <agent> --headless --prompt 'Do the thing'"
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

    label = _uniquify_label(settings, args.label or agent_name)

    request = LaunchRequest(
        agent=agent_name,
        bottles=bottle_names,
        label=label,
        color=args.color or "",
        prompt=prompt,
        headless=True,
        image_policy="cached" if args.cached_images else "fresh",
        assume_yes=True,
        settings=settings,
        cwd=os.getcwd(),
        copy_cwd=args.cwd,
    )
    # Orchestrators relay this PTY and keep its output: stream everything.
    return _launch_bottle(request, manifest, dry_run=dry_run, verbose=True)


def _uniquify_label(settings: Settings, label: str) -> str:
    """Non-interactive analog of `_resolve_unique_label`: if the label's
    slug collides with a running bottle, append -2, -3, … until free,
    logging the chosen label. Orchestrators fire-and-forget many bottles,
    so silently picking a free name beats erroring on every collision."""
    active_slugs = {a.slug for a in enumerate_active_agents(settings)}
    if slugify(label) not in active_slugs:
        return label
    n = 2
    while slugify(f"{label}-{n}") in active_slugs:
        n += 1
    chosen = f"{label}-{n}"
    info(f"label '{label}' already in use; using '{chosen}'")
    return chosen


# --- Launch helpers ------------------------------------------------------


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

    from ...manifest.loader import scan_agent_names
    from ...contract.yaml_subset import YamlSubsetError, parse_frontmatter

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


def _resolve_unique_label(settings: Settings, label: str, color: str) -> tuple[str, str]:
    """Re-prompt with a disclaimer until the label's slug is not already
    in use among running bottles.  Passes through unchanged when no
    collision is found on the first check."""
    while True:
        slug_candidate = slugify(label)
        active_slugs = {a.slug for a in enumerate_active_agents(settings)}
        if slug_candidate not in active_slugs:
            return label, color
        label, color = tui.name_color_modal(
            default_label=label,
            disclaimer=f'"{label}" is already in use',
        )


def _select_image_policy() -> str | None:
    return tui.filter_select(
        ["fresh", "cached"],
        title="Select image startup mode",
    )


def _text_render_preflight():
    def _render(plan: BottlePlan, backend_name: str) -> None:
        print(file=sys.stderr)
        print(f"backend: {backend_name}", file=sys.stderr)
        print(manifest_to_yaml(plan.manifest), file=sys.stderr)
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

    from ...contract.yaml_subset import YamlSubsetError, parse_frontmatter

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


def _launch_bottle(request: LaunchRequest, manifest: ManifestIndex, *, dry_run: bool,
                   verbose: bool = False) -> int:
    """Shared launch core for `start` and `resume`: a local dry run, or the
    request (with its secrets and confirmed host keys) sent to the host
    controller, then attached. ``verbose`` (or `BOT_BOTTLE_VERBOSE=1`)
    streams the launch log instead of one line per stage."""
    try:
        request.check()
    except ValueError as e:
        die(str(e))
    if dry_run:
        return _dry_run(request, manifest)
    interactive = not request.headless
    return launch(prepare_request(request, manifest, interactive=interactive),
                  verbose=verbose or os.environ.get("BOT_BOTTLE_VERBOSE") == "1")


def _dry_run(request: LaunchRequest, manifest: ManifestIndex) -> int:
    """Prepare locally and print the preflight; nothing starts and the
    host controller isn't involved."""
    # pylint: disable=import-outside-toplevel
    from ...backend import BottleSpec, get_bottle_backend

    spec = BottleSpec(
        manifest=manifest,
        agent_name=request.agent,
        copy_cwd=request.copy_cwd,
        user_cwd=request.cwd or os.getcwd(),
        identity=request.identity,
        label=request.label,
        color=request.color,
        bottle_names=request.bottles,
        headless=request.headless,
        image_policy=request.image_policy,
    )
    backend = get_bottle_backend(request.settings, prompt=not request.headless)
    stage_dir = Path(tempfile.mkdtemp(prefix="bot-bottle-stage."))
    identity = ""
    try:
        plan = backend.prepare(spec, stage_dir=stage_dir, secrets=secrets_from_env())
        identity = str(getattr(plan, "slug", ""))
        _text_render_preflight()(plan, backend.name)
        info("dry-run requested; not starting container.")
        return 0
    finally:
        settle_state(identity)
        shutil.rmtree(stage_dir, ignore_errors=True)
