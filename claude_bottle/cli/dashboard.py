"""dashboard: list pending supervise proposals across all bottles and
act on them (approve / modify / reject). PRD 0013 v1.

Curses-based TUI; modify-then-approve shells out to $EDITOR. The
approval handlers wire to the per-tool remediation engines:
PRD 0014 (egress, retargeted from cred-proxy in PRD 0017
chunk 3) writes routes.yaml + SIGHUPs egress; PRD 0015
(pipelock) writes the allowlist + restarts pipelock; PRD 0016
(capability) rebuilds the bottle Dockerfile.
"""

from __future__ import annotations

import argparse
import curses
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import supervise as _supervise
from ..backend import BottleSpec, get_bottle_backend
from ..backend.docker.capability_apply import (
    CapabilityApplyError,
    apply_capability_change,
)
from ..backend.docker.bottle_state import read_metadata
from ..backend.docker.compose import (
    compose_project_name,
    list_active_slugs,
)
from ..backend.docker.egress_apply import (
    EgressApplyError,
    add_route,
    apply_routes_change,
    fetch_current_routes,
)
from ..backend.docker.pipelock_apply import (
    PipelockApplyError,
    apply_allowlist_change,
    fetch_current_allowlist,
    parse_allowlist_content,
    render_allowlist_content,
)
from ..log import info
from ..manifest import Manifest
from ..supervise import (
    ACTION_OPERATOR_EDIT,
    COMPONENT_FOR_TOOL,
    AuditEntry,
    Proposal,
    Response,
    STATUS_APPROVED,
    STATUS_MODIFIED,
    STATUS_REJECTED,
    TOOL_CAPABILITY_BLOCK,
    TOOL_EGRESS_BLOCK,
    TOOL_PIPELOCK_BLOCK,
    archive_proposal,
    list_pending_proposals,
    render_diff,
    write_audit_entry,
    write_response,
)
from ._common import PROG, USER_CWD
from .start import (
    attach_claude,
    capture_session_state,
    prepare_with_preflight,
    settle_state,
)


# Errors any remediation engine may raise. Caught by the TUI key
# handlers and surfaced in the status line so a failed apply keeps
# the proposal pending rather than crashing curses.
ApplyError = (EgressApplyError, PipelockApplyError, CapabilityApplyError)


# --- Discovery -------------------------------------------------------------


@dataclass(frozen=True)
class QueuedProposal:
    """A pending proposal plus the queue dir it was found in."""

    proposal: Proposal
    queue_dir: Path


@dataclass(frozen=True)
class ActiveAgent:
    """One running bottle, as the agents pane displays it (PRD
    0019). `services` is the set of sidecar service names
    currently up for this bottle, used to gate which edit verbs
    apply (no `egress` → `routes edit` is meaningless)."""

    slug: str
    agent_name: str           # from metadata.json; "?" if missing
    started_at: str           # ISO 8601 from metadata.json; "" if missing
    services: tuple[str, ...]  # alphabetical, e.g. ("egress", "pipelock", "supervise")


def _parse_services_by_project(stdout: str) -> dict[str, set[str]]:
    """Parse `docker ps` output formatted as
    `<project-label>\\t<service-label>` (one line per container)
    into a `{project: {service, ...}}` mapping. Pure function for
    testing — the docker invocation is in the caller."""
    out: dict[str, set[str]] = {}
    for line in stdout.splitlines():
        project, _, service = line.partition("\t")
        if not project or not service:
            continue
        out.setdefault(project, set()).add(service)
    return out


def _query_services_by_project() -> dict[str, set[str]]:
    """One `docker ps` call → `{project: {service, ...}}`. PRD
    0019 open question #1 picked this shape over per-bottle
    `compose ps` calls — for hosts with N bottles, this is one
    subprocess instead of N per refresh tick."""
    try:
        r = subprocess.run(
            [
                "docker", "ps",
                "--filter", "label=com.docker.compose.project",
                "--format",
                '{{.Label "com.docker.compose.project"}}'
                "\t"
                '{{.Label "com.docker.compose.service"}}',
            ],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return {}
    if r.returncode != 0:
        return {}
    return _parse_services_by_project(r.stdout or "")


def discover_active_agents() -> list[ActiveAgent]:
    """All currently-running claude-bottle compose projects with
    their metadata + service set. Returns [] when docker isn't
    reachable. PRD 0019."""
    slugs = list_active_slugs()
    if not slugs:
        return []
    services_by_project = _query_services_by_project()
    out: list[ActiveAgent] = []
    for slug in slugs:
        project = compose_project_name(slug)
        services = services_by_project.get(project, set())
        metadata = read_metadata(slug)
        out.append(ActiveAgent(
            slug=slug,
            agent_name=metadata.agent_name if metadata else "?",
            started_at=metadata.started_at if metadata else "",
            services=tuple(sorted(services)),
        ))
    return out




def _approval_status(qp: QueuedProposal, verb: str) -> str:
    """Status-line text after a successful approval. For capability-
    block, append the `resume <identity>` hint so the operator can
    bring the rebuilt bottle back up with one copy-paste."""
    base = f"{verb} {qp.proposal.tool} for [{qp.proposal.bottle_slug}]"
    if qp.proposal.tool == TOOL_CAPABILITY_BLOCK:
        return f"{base}; resume: ./cli.py resume {qp.proposal.bottle_slug}"
    return base


def discover_pending() -> list[QueuedProposal]:
    """Walk ~/.claude-bottle/queue/* and collect pending proposals
    from every bottle's queue. Sorted by arrival time across the
    union — the operator works the global FIFO."""
    queue_root = _supervise.claude_bottle_root() / "queue"
    if not queue_root.is_dir():
        return []
    out: list[QueuedProposal] = []
    for slug_dir in sorted(queue_root.iterdir()):
        if not slug_dir.is_dir():
            continue
        for proposal in list_pending_proposals(slug_dir):
            out.append(QueuedProposal(proposal=proposal, queue_dir=slug_dir))
    out.sort(key=lambda q: q.proposal.arrival_timestamp)
    return out


# --- Operator actions ------------------------------------------------------


def approve(
    qp: QueuedProposal,
    *,
    notes: str = "",
    final_file: str | None = None,
) -> None:
    """Apply the proposal to the running sidecar, write the response
    file the agent's tool call is waiting on, and append an audit
    entry. If `final_file` is provided the status is `modified`;
    otherwise `approved`.

    Raises EgressApplyError if the egress-block apply
    fails (sidecar down, invalid routes content survived the
    operator's modify). On failure no response is written and no
    audit entry is appended — the proposal stays pending so the
    operator can fix the input and retry."""
    status = STATUS_MODIFIED if final_file is not None else STATUS_APPROVED
    file_to_apply = final_file if final_file is not None else qp.proposal.proposed_file

    diff_before, diff_after = "", ""
    if qp.proposal.tool == TOOL_EGRESS_BLOCK:
        # The proposal is a single-route JSON; add_route fetches the
        # current routes from the running egress, merges the
        # new route in, and applies the full merged file. The
        # audit log gets the BEFORE/AFTER of the full file so the
        # diff renders cleanly even though the agent only proposed
        # one entry.
        diff_before, diff_after = add_route(
            qp.proposal.bottle_slug, file_to_apply,
        )
    elif qp.proposal.tool == TOOL_PIPELOCK_BLOCK:
        diff_before, diff_after = _apply_pipelock_url(
            qp.proposal.bottle_slug, file_to_apply,
        )
    elif qp.proposal.tool == TOOL_CAPABILITY_BLOCK:
        diff_before, diff_after = apply_capability_change(
            qp.proposal.bottle_slug, file_to_apply,
        )

    response = Response(
        proposal_id=qp.proposal.id,
        status=status,
        notes=notes,
        final_file=final_file,
    )
    write_response(qp.queue_dir, response)
    _write_audit(
        qp, action=status, notes=notes,
        diff_before=diff_before, diff_after=diff_after,
    )
    if qp.proposal.tool == TOOL_CAPABILITY_BLOCK:
        # The supervise sidecar was torn down by apply_capability_change,
        # so it can't archive its own proposal+response. Archive here so
        # dashboard.discover_pending stops surfacing the resolved
        # proposal forever.
        archive_proposal(qp.queue_dir, qp.proposal.id)


def reject(qp: QueuedProposal, *, reason: str) -> None:
    """Write a rejection response and an audit entry. No remediation
    apply happens on reject — the agent sees the rejection and
    decides whether to retry / give up."""
    response = Response(
        proposal_id=qp.proposal.id,
        status=STATUS_REJECTED,
        notes=reason,
        final_file=None,
    )
    write_response(qp.queue_dir, response)
    _write_audit(qp, action=STATUS_REJECTED, notes=reason, diff_before="", diff_after="")


def operator_edit_routes(slug: str, new_content: str) -> tuple[str, str]:
    """Apply an operator-initiated routes.yaml change (no agent
    proposal). Used by the `routes edit <bottle>` TUI verb and
    available for scripted use. Returns (before, after) like
    apply_routes_change. Writes an audit entry tagged
    ACTION_OPERATOR_EDIT to distinguish from tool-call approvals.

    Raises EgressApplyError on failure."""
    before, after = apply_routes_change(slug, new_content)
    write_audit_entry(AuditEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        bottle_slug=slug,
        component="egress",
        operator_action=ACTION_OPERATOR_EDIT,
        operator_notes="",
        justification="",
        diff=render_diff(before, after, label="egress"),
    ))
    return before, after


def _apply_pipelock_url(slug: str, failed_url: str) -> tuple[str, str]:
    """pipelock-block proposals carry a single failed URL, not a
    full allowlist. Extract the host, merge into the running
    allowlist, and hand the merged content to apply_allowlist_change.
    The full URL (with path) is preserved on the proposal for the
    operator's read; only the host ends up in pipelock's allowlist.

    Pipelock 2.3.0's api_allowlist is hostname-only (verified by
    inspecting the binary's strict preset; the only "path" fields in
    pipelock's schema are about local filesystem paths under sandbox
    / file_sentry / taint). Approving pipelock-block opens the
    entire host, not the URL's path.

    Path-level enforcement was the open question this function's
    earlier docstring flagged; PRD 0017 answered it by putting
    egress in front of pipelock. The agent's
    `egress-block` tool now proposes routes.yaml changes that
    can include a `path_allowlist`. Use that tool for path-level
    follow-ups; this one stays hostname-only because pipelock is
    still the last hostname gate before egress."""
    import urllib.parse
    parsed = urllib.parse.urlsplit(failed_url.strip())
    host = parsed.hostname or ""
    if not host:
        raise PipelockApplyError(
            f"proposed failed_url has no extractable host: {failed_url!r}"
        )
    current = fetch_current_allowlist(slug)
    hosts = parse_allowlist_content(current)
    if host not in hosts:
        hosts.append(host)
    return apply_allowlist_change(slug, render_allowlist_content(hosts))


def operator_edit_allowlist(slug: str, new_content: str) -> tuple[str, str]:
    """Apply an operator-initiated pipelock allowlist change (no
    agent proposal). Used by the `pipelock edit <bottle>` TUI verb
    and available for scripted use. Returns (before, after) like
    apply_allowlist_change. Writes an audit entry tagged
    ACTION_OPERATOR_EDIT to distinguish from tool-call approvals.

    Raises PipelockApplyError on failure."""
    before, after = apply_allowlist_change(slug, new_content)
    write_audit_entry(AuditEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        bottle_slug=slug,
        component="pipelock",
        operator_action=ACTION_OPERATOR_EDIT,
        operator_notes="",
        justification="",
        diff=render_diff(before, after, label="pipelock"),
    ))
    return before, after


def _write_audit(
    qp: QueuedProposal,
    *,
    action: str,
    notes: str,
    diff_before: str,
    diff_after: str,
) -> None:
    """Audit log for egress / pipelock tools. capability-block
    has no audit log (its changes are captured by the bottle's
    rebuild record + git history per PRD 0016).

    For egress-block + pipelock-block approvals the (before,
    after) come from the apply_*_change return — a real
    fetched-from-sidecar diff. For rejections both are empty strings
    and the audit diff renders as empty."""
    component = COMPONENT_FOR_TOOL.get(qp.proposal.tool)
    if component is None:
        return
    write_audit_entry(AuditEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        bottle_slug=qp.proposal.bottle_slug,
        component=component,
        operator_action=action,
        operator_notes=notes,
        justification=qp.proposal.justification,
        diff=render_diff(diff_before, diff_after, label=component),
    ))


# --- $EDITOR integration --------------------------------------------------


def edit_in_editor(content: str, *, suffix: str = ".tmp") -> str | None:
    """Suspend curses (caller is responsible for that), drop `content`
    to a temp file, exec $EDITOR on it, return the edited content.
    Returns None if the edit was a no-op."""
    editor = os.environ.get("EDITOR", "vim")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, prefix="supervise-modify.",
    ) as f:
        f.write(content)
        path = f.name
    try:
        subprocess.run([editor, path], check=False)
        with open(path) as f:
            edited = f.read()
        return edited if edited != content else None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# --- New-agent flow (PRD 0020 chunks 1+2) ----------------------------------
#
# `n` opens a picker modal listing the manifest's agents (with a
# running-count next to each). Selecting one runs prepare → preflight
# (modal) → backend.launch().__enter__() → handoff (curses.endwin →
# claude → refresh). The returned (cm, bottle) lives in the main
# loop's `bottles` dict; chunks 3/4 wire Enter / `x` to act on it.


def _filter_agents(query: str, names: list[str]) -> list[str]:
    """Case-insensitive substring filter for the picker. Pure
    function — no curses, easy to unit-test."""
    if not query:
        return list(names)
    q = query.lower()
    return [n for n in names if q in n.lower()]


def _picker_modal(
    stdscr: "curses._CursesWindow",
    names: list[str],
    running_counts: dict[str, int],
) -> str | None:
    """Modal agent picker. Type to filter; j/k or arrows to
    navigate; Enter to confirm; Esc to abort (first press clears
    filter if any, second press exits)."""
    if not names:
        return None
    selected = 0
    query = ""
    while True:
        filtered = _filter_agents(query, names)
        if not filtered:
            selected = 0
        elif selected >= len(filtered):
            selected = len(filtered) - 1
        elif selected < 0:
            selected = 0

        _draw_picker_modal(stdscr, names, filtered, selected, query, running_counts)
        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return None

        if key == 27:  # Esc
            if query:
                query = ""
                selected = 0
                continue
            return None
        if key in (curses.KEY_ENTER, 10, 13):
            if filtered:
                return filtered[selected]
            continue
        if key in (curses.KEY_DOWN, ord("\x0e")):  # KEY_DOWN, Ctrl-N
            if filtered:
                selected = min(selected + 1, len(filtered) - 1)
            continue
        if key in (curses.KEY_UP, ord("\x10")):  # KEY_UP, Ctrl-P
            if filtered:
                selected = max(selected - 1, 0)
            continue
        if key in (curses.KEY_BACKSPACE, 127, 8):
            query = query[:-1]
            continue
        # Printable character → append to filter
        if 32 <= key < 127:
            query += chr(key)
            continue
        # Anything else: ignore


def _draw_picker_modal(
    stdscr: "curses._CursesWindow",
    all_names: list[str],
    filtered: list[str],
    selected: int,
    query: str,
    running_counts: dict[str, int],
) -> None:
    """Render the picker modal. Width fits the longest name plus
    the `(N running)` suffix; height fits all filtered items plus
    a header line, filter line, and border — capped at 80% of
    screen height with a scrollable inner list if necessary."""
    h, w = stdscr.getmaxyx()
    label_width = max(
        (len(n) for n in all_names), default=10,
    )
    suffix_width = len(" (99 running)")
    inner_width = max(label_width + suffix_width, len("filter: ") + 20, 40)
    box_w = min(inner_width + 4, max(20, w - 4))
    max_list_rows = max(3, int(h * 0.6))
    list_rows = min(len(filtered) if filtered else 1, max_list_rows)
    box_h = list_rows + 5  # border (2) + title (1) + filter (1) + spacer (1)
    box_h = min(box_h, max(7, h - 4))
    top = max(0, (h - box_h) // 2)
    left = max(0, (w - box_w) // 2)

    win = curses.newwin(box_h, box_w, top, left)
    win.erase()
    win.box()
    win.addnstr(0, 2, " start agent ", box_w - 4, curses.A_BOLD)

    win.addnstr(1, 2, f"filter: {query}", box_w - 4)
    win.hline(2, 1, curses.ACS_HLINE, box_w - 2)

    list_start_row = 3
    visible_rows = box_h - list_start_row - 1
    if not filtered:
        win.addnstr(
            list_start_row, 2,
            "(no agents match filter)",
            box_w - 4, curses.A_DIM,
        )
    else:
        # Simple windowing around `selected`.
        first = max(0, selected - visible_rows + 1)
        if selected < first:
            first = selected
        for i, name in enumerate(filtered[first:first + visible_rows]):
            row = list_start_row + i
            count = running_counts.get(name, 0)
            suffix = f"  ({count} running)" if count else ""
            line = f"  {name}{suffix}"
            attr = curses.A_REVERSE if (first + i) == selected else curses.A_NORMAL
            win.addnstr(row, 1, line, box_w - 2, attr)

    win.addnstr(
        box_h - 1, 2,
        " Enter: start  Esc: cancel  type: filter ",
        box_w - 4, curses.A_DIM,
    )
    win.refresh()


def _preflight_modal(
    stdscr: "curses._CursesWindow",
    plan_text: str,
) -> bool:
    """Modal preflight confirmation. `plan_text` is the multi-line
    summary the renderer produced; we draw it in a centered box
    with `[y/N]` at the bottom and capture the next keypress."""
    lines = plan_text.splitlines() or [""]
    h, w = stdscr.getmaxyx()
    inner_width = max(
        max((len(line) for line in lines), default=10),
        len("launch this agent? [y/N]"),
    )
    box_w = min(inner_width + 4, max(20, w - 4))
    box_h = min(len(lines) + 5, max(7, h - 4))
    top = max(0, (h - box_h) // 2)
    left = max(0, (w - box_w) // 2)

    win = curses.newwin(box_h, box_w, top, left)
    win.erase()
    win.box()
    win.addnstr(0, 2, " launch agent ", box_w - 4, curses.A_BOLD)
    for i, line in enumerate(lines[: box_h - 4]):
        win.addnstr(1 + i, 2, line, box_w - 4)
    win.addnstr(
        box_h - 2, 2,
        "launch this agent? [y/N]",
        box_w - 4, curses.A_BOLD,
    )
    win.addnstr(
        box_h - 1, 2,
        " y: launch  N / Esc: abort ",
        box_w - 4, curses.A_DIM,
    )
    win.refresh()

    while True:
        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return False
        if key in (ord("y"), ord("Y")):
            return True
        if key in (ord("n"), ord("N"), 27, curses.KEY_ENTER, 10, 13):
            return False


def _capture_preflight_text(plan) -> str:
    """Capture `plan.print` output by temporarily redirecting
    stderr. Plan rendering is stderr-bound (existing behavior the
    CLI relies on); for the modal we want it as a string."""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        plan.print(remote_control=False)
    return buf.getvalue().strip("\n")


def _running_counts(
    bottles: dict, agents_now: list[ActiveAgent],
) -> dict[str, int]:
    """Per-agent running count: dashboard-owned + externally-
    discovered, summed by agent_name. The picker shows this so the
    operator knows whether picking an agent starts a fresh bottle
    or a Nth one."""
    counts: dict[str, int] = {}
    for a in agents_now:
        counts[a.agent_name] = counts.get(a.agent_name, 0) + 1
    return counts


def _bottle_for_slug(
    slug: str,
    bottles: dict,
    manifest: Manifest | None,
) -> tuple["object", str]:
    """Return `(bottle_handle, prompt_path_hint)` for a re-attach.
    If the slug is in `bottles` (dashboard-owned), return the stored
    handle directly. Otherwise synthesize a `DockerBottle` from the
    container name `claude-bottle-<slug>`. For synthesized bottles
    the prompt-file path comes from the manifest's agent if we can
    resolve it via metadata.json + the loaded manifest; otherwise
    the re-attach runs without `--append-system-prompt-file`.

    Returns the empty string for prompt_path_hint when we omit the
    flag — the caller passes None to DockerBottle in that case."""
    from ..backend.docker.bottle import DockerBottle
    from ..backend.docker.bottle_state import read_metadata
    if slug in bottles:
        _cm, bottle, _identity = bottles[slug]
        return bottle, ""
    # The container hosting the agent's claude process is named
    # `claude-bottle-<slug>` — set by the compose renderer
    # (no service suffix on the agent service, by design).
    container_name = f"claude-bottle-{slug}"
    prompt_path: str | None = None
    metadata = read_metadata(slug)
    if metadata is not None and manifest is not None:
        agent = manifest.agents.get(metadata.agent_name)
        if agent is not None and agent.prompt:
            container_home = os.environ.get(
                "CLAUDE_BOTTLE_CONTAINER_HOME", "/home/node",
            )
            prompt_path = f"{container_home}/.claude-bottle-prompt.txt"
    synth = DockerBottle(
        container=container_name,
        teardown=lambda: None,
        prompt_path_in_container=prompt_path,
    )
    return synth, (prompt_path or "")


def _attach_to_bottle(
    stdscr: "curses._CursesWindow",
    bottle,
    slug: str,
) -> str:
    """Handoff: curses.endwin → attach claude → curses refresh.
    Returns the post-attach status-line message."""
    curses.endwin()
    try:
        exit_code = attach_claude(bottle, remote_control=False)
    except BaseException:
        stdscr.refresh()
        raise
    stdscr.refresh()
    return f"[{slug}] claude session ended (exit {exit_code})"


def _new_agent_flow(
    stdscr: "curses._CursesWindow",
    manifest: Manifest,
    bottles: dict,
    agents_now: list[ActiveAgent],
) -> str:
    """Open the picker, prepare + preflight (modal), launch
    (enter the context manager but DON'T close it), handoff to
    claude. Returns a status-line message for the dashboard footer.
    The (cm, bottle) tuple lands in `bottles` keyed by slug; chunks
    3/4 use it for re-attach and explicit stop."""
    names = sorted(manifest.agents.keys())
    picked = _picker_modal(stdscr, names, _running_counts(bottles, agents_now))
    if picked is None:
        return "agent start aborted"

    spec = BottleSpec(
        manifest=manifest,
        agent_name=picked,
        copy_cwd=False,
        user_cwd=USER_CWD,
    )
    # Modal preflight + prompt. `prepare_with_preflight` calls
    # render_preflight(plan) once, then prompt_yes() to decide. We
    # split the two: render captures the text into a closure, the
    # prompt draws the modal + reads y/N.
    captured: dict[str, str] = {}

    def _render(plan) -> None:
        captured["text"] = _capture_preflight_text(plan)

    def _prompt() -> bool:
        return _preflight_modal(stdscr, captured.get("text", ""))

    stage_dir = Path(tempfile.mkdtemp(prefix="claude-bottle-stage."))
    try:
        plan, identity = prepare_with_preflight(
            spec,
            stage_dir=stage_dir,
            render_preflight=_render,
            prompt_yes=_prompt,
        )
        if plan is None:
            settle_state(identity)
            return f"start of {picked!r} aborted at preflight"

        backend = get_bottle_backend()
        # Launch step writes to stderr (image build, network create,
        # compose up). Get out of curses' way for the duration so
        # the lines render cleanly. The handoff stays endwin'd until
        # claude exits, then we refresh.
        curses.endwin()
        try:
            cm = backend.launch(plan)
            bottle = cm.__enter__()
        except BaseException:
            stdscr.refresh()
            settle_state(identity)
            raise
        bottles[plan.slug] = (cm, bottle, identity)

        try:
            exit_code = attach_claude(bottle, remote_control=False)
            capture_session_state(identity, exit_code)
        finally:
            stdscr.refresh()
        return f"[{plan.slug}] claude session ended (exit {exit_code})"
    finally:
        # stage_dir was the prepare scratch dir; after PRD 0018
        # chunk 2 it holds nothing the running bottle needs. Reap
        # immediately regardless of which branch above ran.
        shutil.rmtree(stage_dir, ignore_errors=True)


# --- TUI -------------------------------------------------------------------


def cmd_dashboard(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} dashboard", add_help=True)
    parser.add_argument(
        "--once", action="store_true",
        help="list pending proposals once and exit (no TUI)",
    )
    args = parser.parse_args(argv)

    if args.once:
        return _list_once()
    try:
        curses.wrapper(_main_loop)
    except KeyboardInterrupt:
        return 130
    return 0


def _list_once() -> int:
    pending = discover_pending()
    if not pending:
        info("no pending proposals")
        return 0
    for qp in pending:
        sys.stdout.write(
            f"{qp.proposal.arrival_timestamp}  "
            f"[{qp.proposal.bottle_slug}]  "
            f"{qp.proposal.tool}  "
            f"{qp.proposal.id}\n"
        )
        sys.stdout.write(f"    {qp.proposal.justification}\n")
    return 0


_REFRESH_INTERVAL_MS = 1000

# How long a newly-arrived proposal stays highlighted (green) in the
# list. Long enough for the operator to notice in their peripheral
# vision, short enough to fade before the queue feels permanently
# noisy.
_NEW_PROPOSAL_HIGHLIGHT_SEC = 5.0


def _is_recent(
    proposal_id: str,
    first_seen: dict[str, float] | None,
    now: float | None,
) -> bool:
    """True if `proposal_id` was first seen within the highlight
    window. Both `first_seen` and `now` may be None (rendered as
    not-recent) so the helper is safe in cold-start paths."""
    if first_seen is None or now is None:
        return False
    started = first_seen.get(proposal_id)
    if started is None:
        return False
    return (now - started) < _NEW_PROPOSAL_HIGHLIGHT_SEC


def _try_init_green() -> int:
    """Initialise a green color pair and return its attr, or 0 if the
    terminal doesn't support color. Caller ORs the returned value
    into addnstr's attr argument; OR 0 is a no-op."""
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        return curses.color_pair(1)
    except curses.error:
        return 0


# PRD 0019 chunk 3: which pane the j/k/arrow keys move through.
# Tab toggles. The proposals pane is the default focus — proposal
# action keys (a/m/r/Enter) require it; agent-scoped keys (e/p,
# chunk 4) require the agents pane.
PANE_PROPOSALS = "proposals"
PANE_AGENTS = "agents"


def _main_loop(stdscr: "curses._CursesWindow") -> None:
    curses.curs_set(0)
    # Auto-refresh: getch() returns -1 after the timeout if no key
    # was pressed, so the loop re-renders with any newly-arrived
    # proposals every ~1s. Without this the screen only updates
    # when the operator hits a key — a tool call landing while the
    # operator is just watching wouldn't appear.
    stdscr.timeout(_REFRESH_INTERVAL_MS)
    green_attr = _try_init_green()
    # Per-proposal first-seen timestamps drive the "new" highlight.
    # We add entries as proposals show up and prune ones that are
    # gone (approved / rejected / archived) so the dict stays small.
    first_seen: dict[str, float] = {}
    selected = 0
    selected_agent = 0
    focus = PANE_PROPOSALS
    status_line = ""
    # PRD 0020: bottles spun up from inside this dashboard session.
    # Each entry: slug -> (context-manager, Bottle handle, identity).
    # We hold the context manager so chunk 4's `x` can call __exit__
    # on it; chunk 5 quit-cleanup intentionally does NOT iterate this
    # dict (the user wants quit to leave bottles running).
    bottles: dict[str, tuple] = {}
    # Manifest is loaded lazily on first `n` so the dashboard
    # doesn't fail to start in a directory with no manifest (e.g.,
    # when the operator is purely watching pre-existing bottles).
    manifest_cache: list[Manifest | None] = [None]

    def _get_manifest() -> Manifest:
        if manifest_cache[0] is None:
            manifest_cache[0] = Manifest.resolve(USER_CWD)
        return manifest_cache[0]
    while True:
        pending = discover_pending()
        if selected >= len(pending):
            selected = max(0, len(pending) - 1)

        agents = discover_active_agents()
        if selected_agent >= len(agents):
            selected_agent = max(0, len(agents) - 1)

        now = time.monotonic()
        live_ids = {qp.proposal.id for qp in pending}
        for proposal_id in live_ids:
            first_seen.setdefault(proposal_id, now)
        for stale_id in list(first_seen.keys() - live_ids):
            del first_seen[stale_id]

        _render(
            stdscr, pending, selected, status_line,
            agents=agents,
            selected_agent=selected_agent,
            focus=focus,
            first_seen=first_seen, now=now, green_attr=green_attr,
        )

        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return

        if key == -1:
            # Timeout fired — re-render with fresh queue. Status_line
            # is left intact so messages from a prior keystroke stay
            # readable until the operator actually does something else.
            continue

        # Real keystroke: clear any stale status before dispatching
        # so the next render reflects what just happened.
        status_line = ""

        if key in (ord("q"), 27):  # q or ESC
            return
        if key == 9:  # Tab
            focus = PANE_AGENTS if focus == PANE_PROPOSALS else PANE_PROPOSALS
            continue
        if key == ord("n"):
            # PRD 0020 chunk 2: open the picker, start + attach to
            # the chosen agent, return to the dashboard with the
            # bottle running.
            try:
                manifest = _get_manifest()
            except Exception as e:
                status_line = f"manifest load failed: {e}"
                continue
            status_line = _new_agent_flow(stdscr, manifest, bottles, agents)
            continue
        if key in (ord("e"), ord("p")):
            # PRD 0019 chunk 4: agent-scoped edits. Only fire when
            # the agents pane is focused on a real selection;
            # otherwise no-op with a status hint. The pre-PRD
            # discover-and-prompt scaffolding is gone.
            selected_obj = _selected_agent(focus, agents, selected_agent)
            if selected_obj is None:
                status_line = "no agent selected; Tab into the agents pane first"
                continue
            if key == ord("e"):
                status_line = _operator_edit_routes_flow(stdscr, selected_obj)
            else:
                status_line = _operator_edit_allowlist_flow(stdscr, selected_obj)
            continue

        if focus == PANE_AGENTS:
            # j/k/arrow navigate the agents list. Enter re-attaches
            # to the focused bottle (PRD 0020 chunk 3) — works for
            # both dashboard-owned bottles and bottles discovered
            # via `list_active_slugs` (the synth path resolves the
            # in-container claude target from the slug).
            if key in (curses.KEY_DOWN, ord("j")):
                selected_agent = min(selected_agent + 1, max(0, len(agents) - 1))
            elif key in (curses.KEY_UP, ord("k")):
                selected_agent = max(selected_agent - 1, 0)
            elif key in (curses.KEY_ENTER, 10, 13):
                target = _selected_agent(focus, agents, selected_agent)
                if target is None:
                    status_line = "no agent selected"
                else:
                    manifest = manifest_cache[0]  # may be None; that's ok
                    bottle, _hint = _bottle_for_slug(target.slug, bottles, manifest)
                    status_line = _attach_to_bottle(stdscr, bottle, target.slug)
            continue

        if not pending:
            continue
        qp = pending[selected]

        if key in (curses.KEY_DOWN, ord("j")):
            selected = min(selected + 1, len(pending) - 1)
        elif key in (curses.KEY_UP, ord("k")):
            selected = max(selected - 1, 0)
        elif key in (curses.KEY_ENTER, 10, 13, ord("v")):
            _detail_view(stdscr, qp, green_attr=green_attr)
        elif key == ord("a"):
            try:
                approve(qp)
                status_line = _approval_status(qp, "approved")
            except ApplyError as e:
                status_line = f"apply failed: {e}"
        elif key == ord("m"):
            edited = _modify(stdscr, qp)
            if edited is None:
                status_line = "modify aborted (no change)"
            else:
                try:
                    approve(qp, final_file=edited, notes="operator modified before approving")
                    status_line = _approval_status(qp, "modified+approved")
                except ApplyError as e:
                    status_line = f"apply failed: {e}"
        elif key == ord("r"):
            reason = _prompt(stdscr, "reject reason: ")
            if reason:
                reject(qp, reason=reason)
                status_line = f"rejected {qp.proposal.tool} for [{qp.proposal.bottle_slug}]"
            else:
                status_line = "reject aborted (empty reason)"


def _render(
    stdscr: "curses._CursesWindow",
    pending: list[QueuedProposal],
    selected: int,
    status_line: str,
    *,
    agents: list[ActiveAgent] | None = None,
    selected_agent: int = 0,
    focus: str = PANE_PROPOSALS,
    first_seen: dict[str, float] | None = None,
    now: float | None = None,
    green_attr: int = 0,
) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    agents = agents or []
    header = (
        f"claude-bottle dashboard  "
        f"({len(pending)} pending, {len(agents)} active)"
    )
    stdscr.addnstr(0, 0, header, w - 1, curses.A_BOLD)
    stdscr.hline(1, 0, curses.ACS_HLINE, w)

    proposals_focused = focus == PANE_PROPOSALS
    agents_focused = focus == PANE_AGENTS

    # ----- proposals pane (top) -----
    row = 2
    proposals_label = "proposals:"
    if proposals_focused:
        proposals_label += "  (focused)"
    stdscr.addnstr(row, 0, proposals_label, w - 1, curses.A_DIM)
    row += 1
    if not pending:
        stdscr.addnstr(
            row, 2,
            "no pending proposals; agents will queue here when they call a "
            "supervise tool",
            w - 4,
        )
        row += 1
    else:
        for i, qp in enumerate(pending):
            if row >= h - 4 - max(1, len(agents) + 2):
                break
            p = qp.proposal
            ts_short = (
                p.arrival_timestamp.split("T", 1)[1][:8]
                if "T" in p.arrival_timestamp else p.arrival_timestamp
            )
            cursor = "> " if (proposals_focused and i == selected) else "  "
            line = (
                f"{cursor}"
                f"[{p.bottle_slug}] {p.tool:<20} {ts_short}  "
                f"{p.justification[:60]}"
            )
            attr = (
                curses.A_REVERSE
                if (proposals_focused and i == selected)
                else curses.A_NORMAL
            )
            if _is_recent(p.id, first_seen, now):
                attr |= green_attr
            stdscr.addnstr(row, 0, line, w - 1, attr)
            row += 1

    # ----- agents pane (bottom) -----
    # One blank-line separator + an "active agents:" label, then
    # one row per agent. Reverse-video the selected row when this
    # pane has focus. Stops before the status / footer area so
    # they always stay visible.
    row += 1
    agents_label = "active agents:"
    if agents_focused:
        agents_label += "  (focused)"
    if row < h - 3:
        stdscr.addnstr(row, 0, agents_label, w - 1, curses.A_DIM)
        row += 1
    if not agents:
        if row < h - 3:
            stdscr.addnstr(
                row, 2,
                "no active bottles; ./cli.py start <agent>",
                w - 4, curses.A_DIM,
            )
    else:
        for i, a in enumerate(agents):
            if row >= h - 3:
                break
            line = _format_agent_row(a, w - 1)
            if agents_focused and i == selected_agent:
                # Replace the leading "  " cursor with "> " and
                # highlight the whole row.
                line = "> " + line[2:]
                attr = curses.A_REVERSE
            else:
                attr = curses.A_NORMAL
            stdscr.addnstr(row, 0, line, w - 1, attr)
            row += 1

    footer = (
        "[n] new  [Tab] switch  [j/k] move  "
        "[Enter] view/attach  [a/m/r] proposal  [e/p] edit  [q] quit"
    )
    stdscr.hline(h - 2, 0, curses.ACS_HLINE, w)
    stdscr.addnstr(h - 1, 0, footer, w - 1, curses.A_DIM)
    if status_line:
        stdscr.addnstr(h - 3, 0, status_line, w - 1, curses.A_BOLD)
    else:
        # When idle: surface which agent is currently selected so
        # the operator knows what `e` / `p` will target after chunk
        # 4 wires the agent-scoped edit verbs.
        sel = _selection_status(focus, agents, selected_agent)
        if sel:
            stdscr.addnstr(h - 3, 0, sel, w - 1, curses.A_DIM)
    stdscr.refresh()


def _selection_status(
    focus: str, agents: list[ActiveAgent], selected_agent: int,
) -> str:
    """Status-line text for the idle state. Surfaces the agents-
    pane selection so the operator can tell what an agent-scoped
    edit verb would target."""
    if focus != PANE_AGENTS:
        return ""
    if not agents:
        return "[no active agents]"
    if 0 <= selected_agent < len(agents):
        return f"[selected: {agents[selected_agent].slug}]"
    return "[no agent selected]"


def _selected_agent(
    focus: str, agents: list[ActiveAgent], selected_agent: int,
) -> ActiveAgent | None:
    """The selected agent to scope `e` / `p` to, or None if no
    selection is valid (proposals pane focused, no active agents,
    or selection out of bounds)."""
    if focus != PANE_AGENTS:
        return None
    if not agents:
        return None
    if 0 <= selected_agent < len(agents):
        return agents[selected_agent]
    return None


def _format_agent_row(a: ActiveAgent, maxw: int) -> str:
    """One-line agent row: `  <slug> <agent_name> started <HH:MM:SS>
    [<sidecars>]`. The `agent` service is filtered out of the
    displayed list — it's always present for an active bottle, so
    listing it carries no information; the sidecars are the
    differentiator. Truncated to `maxw` because the renderer's
    addnstr only enforces width if we hand it a properly-sized
    string."""
    started = (
        a.started_at.split("T", 1)[1][:8]
        if "T" in a.started_at else (a.started_at or "?")
    )
    sidecars = tuple(s for s in a.services if s != "agent")
    services = ",".join(sidecars) if sidecars else "(starting)"
    line = (
        f"  {a.slug}  {a.agent_name}  "
        f"started {started}  [{services}]"
    )
    if len(line) > maxw:
        return line[: max(0, maxw - 1)] + "…"
    return line


def _detail_view(
    stdscr: "curses._CursesWindow",
    qp: QueuedProposal,
    *,
    green_attr: int = 0,
) -> None:
    """Render the full proposal: header, justification, proposed file
    contents. Scrollable. Press q to return."""
    lines = _detail_lines(qp, green_attr=green_attr)
    offset = 0
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        for i, (text, attr) in enumerate(lines[offset:offset + h - 1]):
            stdscr.addnstr(i, 0, text, w - 1, attr)
        stdscr.addnstr(
            h - 1, 0,
            "[j/k] scroll  [g/G] top/bottom  [a] approve  [m] modify  [r] reject  [q] back",
            w - 1, curses.A_DIM,
        )
        stdscr.refresh()
        key = stdscr.getch()
        if key in (ord("q"), 27):
            return
        if key in (curses.KEY_DOWN, ord("j")):
            offset = min(offset + 1, max(0, len(lines) - 1))
        elif key in (curses.KEY_UP, ord("k")):
            offset = max(offset - 1, 0)
        elif key == ord("g"):
            offset = 0
        elif key == ord("G"):
            offset = max(0, len(lines) - 1)
        elif key == ord("a"):
            try:
                approve(qp)
            except ApplyError:
                pass  # Status surfaces back in the list view's render.
            return
        elif key == ord("m"):
            edited = _modify(stdscr, qp)
            if edited is not None:
                try:
                    approve(qp, final_file=edited, notes="operator modified before approving")
                except ApplyError:
                    pass
            return
        elif key == ord("r"):
            reason = _prompt(stdscr, "reject reason: ")
            if reason:
                reject(qp, reason=reason)
            return


def _detail_lines(
    qp: QueuedProposal,
    *,
    green_attr: int = 0,
) -> list[tuple[str, int]]:
    """Return the detail-view body as (text, curses-attr) tuples.
    Most lines are plain (attr=0); pipelock-block proposals append
    a green "→ would allow host: ..." line so the operator sees at
    a glance which hostname will land in pipelock's allowlist if
    they hit approve. The URL itself is shown above for context."""
    p = qp.proposal
    out: list[tuple[str, int]] = [
        (f"bottle: {p.bottle_slug}", 0),
        (f"tool: {p.tool}", 0),
        (f"id: {p.id}", 0),
        (f"arrived: {p.arrival_timestamp}", 0),
        (f"queue: {qp.queue_dir}", 0),
        ("", 0),
        ("justification:", 0),
    ]
    out.extend(("  " + line, 0) for line in p.justification.splitlines() or [""])
    out.extend([
        ("", 0),
        (_proposed_payload_label(p.tool) + ":", 0),
    ])
    out.extend((line, 0) for line in p.proposed_file.splitlines() or [""])
    if p.tool == TOOL_PIPELOCK_BLOCK:
        host = _failed_url_host(p.proposed_file)
        if host:
            # Show the literal line that will be appended to the
            # bottle's pipelock allowlist on approve. Green so it
            # reads as "what changes"; the URL above carries the
            # path context (which pipelock can't enforce — see the
            # follow-up note on _apply_pipelock_url).
            out.append(("", 0))
            out.append((host, green_attr))
    return out


def _failed_url_host(url: str) -> str:
    """Best-effort hostname extraction from a pipelock-block proposal's
    failed_url payload. Returns empty string on unparseable input —
    callers handle empty as "nothing to highlight"."""
    import urllib.parse
    try:
        return urllib.parse.urlsplit(url.strip()).hostname or ""
    except ValueError:
        return ""


def _proposed_payload_label(tool: str) -> str:
    """The detail-view section heading for the proposal's payload —
    `proposed_file` is what the dataclass calls it, but for
    pipelock-block the payload is a single URL not a file. Render
    the label per tool so the operator's eye matches."""
    if tool == TOOL_PIPELOCK_BLOCK:
        return "failed URL"
    return "proposed file"


def _modify(stdscr: "curses._CursesWindow", qp: QueuedProposal) -> str | None:
    """Suspend curses, open $EDITOR on the proposed file, return the
    edited content (or None if unchanged)."""
    suffix = _suffix_for_tool(qp.proposal.tool)
    curses.endwin()
    try:
        edited = edit_in_editor(qp.proposal.proposed_file, suffix=suffix)
    finally:
        stdscr.refresh()
    return edited


def _suffix_for_tool(tool: str) -> str:
    if tool == TOOL_CAPABILITY_BLOCK:
        return ".dockerfile"
    # egress-block / pipelock-block: JSON-ish + plain.
    return ".txt"


def _operator_edit_routes_flow(
    stdscr: "curses._CursesWindow", agent: ActiveAgent,
) -> str:
    """Operator-initiated routes.yaml edit, scoped to `agent`.
    PRD 0019: selection in the agents pane is the only way to
    invoke this — the discover-and-prompt scaffolding is gone.
    Refuses if the agent has no running egress sidecar."""
    return _operator_edit_flow(
        stdscr,
        agent=agent,
        required_service="egress",
        label="routes",
        fetch=fetch_current_routes,
        apply=operator_edit_routes,
        suffix=".yaml",
    )


def _operator_edit_allowlist_flow(
    stdscr: "curses._CursesWindow", agent: ActiveAgent,
) -> str:
    """Operator-initiated pipelock allowlist edit, scoped to `agent`.
    Pipelock is always present on an active bottle (no toggle in the
    manifest) so the required-service check is belt-and-braces but
    surfaces a clear error in the race-window case where compose up
    is mid-flight."""
    return _operator_edit_flow(
        stdscr,
        agent=agent,
        required_service="pipelock",
        label="pipelock",
        fetch=fetch_current_allowlist,
        apply=operator_edit_allowlist,
        suffix=".txt",
    )


def _operator_edit_flow(
    stdscr: "curses._CursesWindow",
    *,
    agent: ActiveAgent,
    required_service: str,
    label: str,
    fetch,
    apply,
    suffix: str,
) -> str:
    """Shared scaffolding for the routes-edit + pipelock-edit verbs.
    `fetch(slug)` returns the current operator-facing config;
    `apply(slug, new)` does the write + restart/SIGHUP and writes
    the audit entry."""
    if required_service not in agent.services:
        return (
            f"[{agent.slug}] has no running {required_service} sidecar; "
            f"nothing to edit"
        )
    slug = agent.slug
    try:
        current = fetch(slug)
    except ApplyError as e:
        return f"fetch failed: {e}"
    curses.endwin()
    try:
        edited = edit_in_editor(current, suffix=suffix)
    finally:
        stdscr.refresh()
    if edited is None:
        return f"{label} for [{slug}] unchanged"
    try:
        apply(slug, edited)
    except ApplyError as e:
        return f"apply failed: {e}"
    return f"updated {label} for [{slug}]"


def _prompt(stdscr: "curses._CursesWindow", label: str) -> str:
    """One-line input at the bottom of the screen."""
    curses.curs_set(1)
    h, _ = stdscr.getmaxyx()
    stdscr.move(h - 2, 0)
    stdscr.clrtoeol()
    stdscr.addstr(h - 2, 0, label)
    stdscr.refresh()
    curses.echo()
    try:
        raw = stdscr.getstr(h - 2, len(label), 200)
    finally:
        curses.noecho()
        curses.curs_set(0)
    return raw.decode("utf-8", errors="replace").strip()


__all__ = [
    "ACTION_OPERATOR_EDIT",  # re-exported for 0014/0015 to write operator-initiated audit entries
    "QueuedProposal",
    "approve",
    "cmd_dashboard",
    "discover_pending",
    "edit_in_editor",
    "reject",
]
