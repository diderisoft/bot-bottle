"""start: launch a bottle for a named agent and attach to its session.

On a terminal, without `--bottle`, `start` is guided (`guided.py`): pick a
harness, describe the task, get a suggested bottle, run it (`--auto` runs it
without asking; `BOT_BOTTLE_START_AUTO=1` makes that the default and
`--no-auto` asks again for one run). Without a terminal nothing prompts:
`--auto "<task>"` runs a suggestion, otherwise give `--bottle`.

The CLI resolves everything interactive (pickers, label, color, secrets, host
keys, confirmations), then asks the host controller to launch. The agent runs
in a controller-owned tmux session. Ctrl-] detaches (`bot-bottle attach <id>`
rejoins); the bottle is torn down when the agent exits, and `start` exits with
its code.

`--headless` takes everything from flags and skips prompts, still attaching on
the inherited PTY. `--dry-run` prepares locally and prints the preflight.

Env values (`SECRETS_HELP`): `?` asks at every launch, `${VAR}` reads the
environment, `secret:NAME` the secrets file; `secret:` marks a secret.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from ...backend import BottlePlan, launch_isolation_note
from ...bottle_state import settle_state
from ...contract.hostctl import LaunchRequest, Settings
from ...lib.log import info, die, warn
from ...manifest import ManifestIndex
from ...manifest.loader import PROJECT_PREFIX
from ...manifest.schema import DEFAULT_BOTTLE
from ...manifest.preview import manifest_to_yaml
from ..constants import PROG
from .guided import guided_start, wants_guided
from ..launch_client import launch, prepare_request
from ..secrets_env import secrets_from_env
from ..settings import settings_from_env
from .. import screen, tui

if TYPE_CHECKING:
    from ..screen import Screen, Step


SECRETS_HELP = (
    "Values in a bottle's env: `?` (or `?<message>`) asks at every launch; `${VAR}` "
    "reads VAR from this shell's environment; `secret:NAME` reads NAME from the "
    "secrets file (~/.bot-bottle/secrets), asking once and offering to save it when "
    "it's missing. `secret:` marks a secret (hidden when typed, scanned for in pushes, "
    "masked in transcripts): `secret:?` and `secret:${VAR}` are the secret forms of the "
    "first two. A route's auth is `{ type: bearer|token|header, value: secret:NAME }` "
    "(`name: <header>` too for a header). Without a terminal nothing asks: a `?` uses "
    "a saved value of the same name, with a warning.")


def cmd_start(argv: list[str]) -> int:  # pylint: disable=too-many-return-statements
    parser = argparse.ArgumentParser(prog=f"{PROG} start", add_help=True, epilog=SECRETS_HELP)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help=("stream the full launch log instead of one line per stage "
                              "(it's always saved under ~/.bot-bottle/logs/)"))
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
            "bottle to compose, repeatable (order = merge order); skips the "
            f"bottle picker. In --headless, defaults to `{DEFAULT_BOTTLE}` when omitted."
        ),
    )
    parser.add_argument(
        "--label",
        default=None,
        help="bottle label / terminal title (default: the agent's name); skips "
             "asking for one with --pick",
    )
    parser.add_argument(
        "--color",
        default=None,
        help="bottle color, one of the 16 ANSI color names (default: none)",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="the agent's task: its first message in an interactive session, or "
             "what a --headless one runs (required there)",
    )
    auto = parser.add_mutually_exclusive_group()
    auto.add_argument("--auto", action="store_true", default=None,
                      help="run the suggested bottle without asking (it isn't saved)")
    auto.add_argument("--no-auto", dest="auto", action="store_false",
                      help="ask before running the suggested bottle, despite "
                           "BOT_BOTTLE_START_AUTO")
    parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help=("agent name from ~/.bot-bottle/agents or ./.bot-bottle/agents "
              "(omit to use the last one, or pick)"),
    )
    parser.add_argument("task", nargs="?", default=None,
                        help="the task, for a suggested bottle (same as --prompt)")
    args = parser.parse_args(argv)
    if args.cached_images and not args.headless:
        die("--cached-images is only supported with --headless")

    dry_run = args.dry_run
    settings = settings_from_env()
    if args.no_cache:
        settings = replace(
            settings, operation=replace(settings.operation, no_cache=True))

    manifest = ManifestIndex.resolve(os.getcwd())
    _split_task(args, manifest)

    if args.headless:
        return _start_headless(manifest, args, settings, dry_run=dry_run)
    if args.auto is None:
        args.auto = os.environ.get("BOT_BOTTLE_START_AUTO", "") == "1"
    if not args.bottle and (wants_guided(args) or args.auto):

        def go(request: LaunchRequest, index: ManifestIndex, *, view: "Screen | None" = None,
               lead: "list[Step] | None" = None) -> int:
            if not sys.stdin.isatty():
                request = replace(request, headless=True, assume_yes=True)
            return _launch_bottle(request, index, dry_run=dry_run, verbose=args.verbose,
                                  view=view, lead=lead)
        if not sys.stdin.isatty() and not (args.prompt or args.task):
            die("without a terminal, --auto needs the task: "
                f"{PROG} start [<agent>] --auto \"<task>\"")
        if not (args.auto or args.verbose or dry_run) and screen.usable():
            # pylint: disable-next=import-outside-toplevel
            from .guided_view import run_start
            return run_start(args, manifest, settings, launch=go)
        return guided_start(args, manifest, settings, launch=go, auto=args.auto)

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

    # Bottles are picked at launch; agents don't name one (ADR 0011).
    if args.bottle:
        bottle_names: tuple[str, ...] = tuple(args.bottle)
    else:
        picked = _pick_bottles(manifest)
        if picked is None:
            return 0
        bottle_names = picked

    # `--pick` asks for a label (unless `--label` gave one), color and image
    # mode.
    if args.pick and not args.label:
        label, color = tui.name_color_modal(default_label=agent_name)
    else:
        label, color = args.label or agent_name, args.color or ""
    if args.pick:
        image_policy = _select_image_policy()
        if image_policy is None:
            return 0
    else:
        image_policy = "prefer-cached"

    request = LaunchRequest(
        agent=agent_name,
        bottles=bottle_names,
        label=label,
        color=color,
        image_policy=image_policy,
        # The agent's first message, as a console launch delivers it.
        prompt=args.prompt or "",
        settings=settings,
        cwd=os.getcwd(),
        copy_cwd=args.cwd,
    )
    return _launch_bottle(request, manifest, dry_run=dry_run, verbose=args.verbose)


def _split_task(args: argparse.Namespace, manifest: ManifestIndex) -> None:
    """`start "<task>"`: a first argument that isn't an agent is the task."""
    if args.name and args.task is None and args.name not in manifest.all_agent_names \
            and (" " in args.name or args.auto):
        args.name, args.task = None, args.name
    if args.task is not None:
        if args.prompt is not None:
            die("give the task once: as an argument or with --prompt")
        args.prompt = args.task


def _pick_bottles(manifest: ManifestIndex) -> tuple[str, ...] | None:
    """The bottles picked to compose, or None when the picker is cancelled.
    `DEFAULT_BOTTLE` is preselected, and picking none launches in it too. A
    project's own bottles (`./<name>`) come first, marked as this project's."""
    available_bottles = sorted(manifest.all_bottle_names,
                               key=lambda n: (not n.startswith(PROJECT_PREFIX), n))
    project_mark = f"  · this project ({Path(os.getcwd()).name})"
    lineage_map = {n: n + project_mark for n in available_bottles
                   if n.startswith(PROJECT_PREFIX)}
    lineage_map.update(_bottle_lineage(manifest))
    display_labels = [lineage_map.get(n, n) for n in available_bottles]
    label_to_name = {lineage_map.get(n, n): n for n in available_bottles}
    initial_labels = ([lineage_map.get(DEFAULT_BOTTLE, DEFAULT_BOTTLE)]
                      if DEFAULT_BOTTLE in available_bottles else [])
    selected_labels = tui.filter_multiselect(
        display_labels,
        title="Select bottles",
        initial=initial_labels,
    )
    if selected_labels is None:
        return None
    return tuple(label_to_name.get(lbl, lbl) for lbl in selected_labels)


# --- Headless launch ---


def _start_headless(
    manifest: ManifestIndex,
    args: argparse.Namespace,
    settings: Settings,
    *,
    dry_run: bool,
) -> int:
    """Non-interactive launch: agent, bottles, label and color from flags and
    manifest defaults; the preflight is auto-confirmed. With a terminal it
    attaches; without one (a service, CI, a webhook) it reports the session
    and returns, after watching long enough to report an agent that fails at
    once."""
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

    bottle_names: tuple[str, ...] = tuple(args.bottle or (DEFAULT_BOTTLE,))

    label = args.label or agent_name

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
    # Stream everything; callers relay this PTY.
    return _launch_bottle(request, manifest, dry_run=dry_run, verbose=True)


# --- Launch helpers ---


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
        note = launch_isolation_note(plan, backend_name)
        if note:
            warn(note)
    return _render


def _bottle_lineage(manifest: ManifestIndex) -> dict[str, str]:
    """{bottle: lineage label} for bottles with an extends chain, root first
    (e.g. 'dev -> bot-bottle-dev -> claude-dev')."""
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
                   verbose: bool = False, view: "Screen | None" = None,
                   lead: "list[Step] | None" = None) -> int:
    """Shared launch for `start` and `resume`: a local dry run, or the request
    (with secrets and confirmed host keys) sent to the host controller and
    attached. `verbose` streams the launch log."""
    try:
        request.check()
    except ValueError as e:
        die(str(e))
    if dry_run:
        return _dry_run(request, manifest)
    interactive = not request.headless
    if view is not None:  # a guided start's screen: its stages draw there
        # Trust, host-key and secret prompts ask on it, inline (`lib.prompt`).
        prepared = prepare_request(request, manifest, interactive=interactive)
        return launch(prepared, verbose=verbose, view=view, lead=lead)
    prepared = prepare_request(request, manifest, interactive=interactive)
    return launch(prepared, verbose=verbose)


def _dry_run(request: LaunchRequest, manifest: ManifestIndex) -> int:
    """Prepare locally and print the preflight; nothing starts. The host's
    audit default comes from the host controller, which holds the host DB."""
    # pylint: disable=import-outside-toplevel
    from ...backend import BottleSpec, get_bottle_backend
    from ..host_state_client import host_config

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
        default_audit_delivery=host_config().audit_delivery,
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
