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
import contextlib
import curses
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import supervise as _supervise
from ..agent_provider import runtime_for
from ..backend import (
    ActiveAgent,
    BottleSpec,
    enumerate_active_agents,
    get_bottle_backend,
    known_backend_names,
)
from ..backend.docker.capability_apply import (
    CapabilityApplyError,
    apply_capability_change,
)
from ..backend.docker.bottle_state import bottle_state_dir, read_metadata
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
from ..log import Die, error, info
from ..manifest import Manifest, ManifestError
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
    attach_agent,
    capture_claude_session_state,
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


def discover_active_agents() -> list[ActiveAgent]:
    """All currently-running agents across every backend with
    their metadata + service set. Returns [] when neither
    backend is reachable. Backed by the shared
    `enumerate_active_agents` helper so the CLI's
    `./cli.py list active` and this dashboard show the same data."""
    return enumerate_active_agents()




def _approval_status(qp: QueuedProposal, verb: str) -> str:
    """Status-line text after a successful approval. For capability-
    block, append the `resume <identity>` hint so the operator can
    bring the rebuilt bottle back up with one copy-paste."""
    base = f"{verb} {qp.proposal.tool} for [{qp.proposal.bottle_slug}]"
    if qp.proposal.tool == TOOL_CAPABILITY_BLOCK:
        return f"{base}; resume: ./cli.py resume {qp.proposal.bottle_slug}"
    return base


def discover_pending() -> list[QueuedProposal]:
    """Walk ~/.bot-bottle/queue/* and collect pending proposals
    from every bottle's queue. Sorted by arrival time across the
    union — the operator works the global FIFO."""
    queue_root = _supervise.bot_bottle_root() / "queue"
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
        _meta = read_metadata(qp.proposal.bottle_slug)
        if _meta is not None and not _meta.compose_project:
            raise CapabilityApplyError(
                "capability-block remediation is not supported for smolmachines "
                "bottles. Reject this proposal or handle the capability change "
                "manually, then restart the bottle."
            )
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
            _erase_modal(stdscr)
            return None

        if key == 27:  # Esc
            if query:
                query = ""
                selected = 0
                continue
            _erase_modal(stdscr)
            return None
        if key in (curses.KEY_ENTER, 10, 13):
            if filtered:
                _erase_modal(stdscr)
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
        empty_message = (
            "(no agents configured)"
            if not all_names else "(no agents match filter)"
        )
        win.addnstr(
            list_start_row, 2,
            empty_message,
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
            _erase_modal(stdscr)
            return False
        if key in (ord("y"), ord("Y")):
            _erase_modal(stdscr)
            return True
        if key in (ord("n"), ord("N"), 27, curses.KEY_ENTER, 10, 13):
            _erase_modal(stdscr)
            return False


def _backend_picker_modal(
    stdscr: "curses._CursesWindow",
    agent_name: str,
) -> str | None:
    """Modal "which backend to launch this agent on?" picker. Up/
    Down + Enter to confirm, Esc / N to abort. Returns the chosen
    backend name or None on abort.

    Defaults to the first known backend (`docker` lexicographically),
    which keeps existing-muscle-memory flows quiet — the modal only
    surfaces a choice; it doesn't surprise the operator by jumping
    to smolmachines. The picker exists so operators can opt in to
    smolmachines without setting BOT_BOTTLE_BACKEND beforehand
    (issue #77)."""
    names = list(known_backend_names())
    if len(names) <= 1:
        return names[0] if names else None
    selected = 0
    h, w = stdscr.getmaxyx()
    box_w = min(60, max(20, w - 4))
    box_h = min(len(names) + 6, max(8, h - 4))
    top = max(0, (h - box_h) // 2)
    left = max(0, (w - box_w) // 2)

    while True:
        win = curses.newwin(box_h, box_w, top, left)
        win.erase()
        win.box()
        win.addnstr(0, 2, " choose backend ", box_w - 4, curses.A_BOLD)
        win.addnstr(
            1, 2,
            f"launching {agent_name!r}; pick a backend:",
            box_w - 4,
        )
        for i, name in enumerate(names):
            marker = "▶" if i == selected else " "
            attr = curses.A_REVERSE if i == selected else 0
            win.addnstr(3 + i, 2, f"{marker} {name}", box_w - 4, attr)
        win.addnstr(
            box_h - 2, 2,
            " Enter: confirm   Esc / N: abort   ↑/↓: move ",
            box_w - 4, curses.A_DIM,
        )
        win.refresh()

        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            _erase_modal(stdscr)
            return None
        if key in (curses.KEY_UP,):
            selected = (selected - 1) % len(names)
        elif key in (curses.KEY_DOWN,):
            selected = (selected + 1) % len(names)
        elif key in (curses.KEY_ENTER, 10, 13):
            _erase_modal(stdscr)
            return names[selected]
        elif key in (ord("n"), ord("N"), 27):
            _erase_modal(stdscr)
            return None


def _erase_modal(stdscr: "curses._CursesWindow") -> None:
    """Force-redraw the dashboard's pre-modal frame so a modal
    sub-window's content stops showing. Curses tracks the modal
    via the newwin sub-window we created; touchwin + refresh
    on stdscr repaints stdscr's last buffered frame over the
    sub-window's area. Without this, the modal stays on screen
    until the dashboard's main loop ticks again — which during
    a long-running launch is several seconds away."""
    stdscr.touchwin()
    stdscr.refresh()


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
    container name `bot-bottle-<slug>`. For synthesized bottles
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
    # The container hosting the agent's agent process is named
    # `bot-bottle-<slug>` — set by the compose renderer
    # (no service suffix on the agent service, by design).
    container_name = f"bot-bottle-{slug}"
    prompt_path: str | None = None
    metadata = read_metadata(slug)
    if metadata is not None and manifest is not None:
        agent = manifest.agents.get(metadata.agent_name)
        if agent is not None and agent.prompt:
            container_home = os.environ.get(
                "BOT_BOTTLE_CONTAINER_HOME", "/home/node",
            )
            prompt_path = f"{container_home}/.bot-bottle-prompt.txt"
    synth = DockerBottle(
        container=container_name,
        teardown=lambda: None,
        prompt_path_in_container=prompt_path,
    )
    return synth, (prompt_path or "")


def _stop_bottle_flow(
    stdscr: "curses._CursesWindow",
    bottles: dict,
    slug: str,
    *,
    tmux_state: dict | None = None,
) -> str:
    """Explicit per-bottle teardown (PRD 0020 chunk 4). Pops the
    (cm, bottle, identity) tuple from the dashboard's bottles
    map, snapshots the transcript best-effort, drives the launch
    context's __exit__ (compose down + network remove), and
    settles the state dir. A non-owned slug is a no-op with a
    hint pointing at `./cli.py cleanup`.

    PRD 0021: clears `tmux_state['slug']` when the stopped
    bottle was the right-pane occupant. The pane itself is
    left in place — the operator presses Enter on a different
    agent to repurpose it (respawn-pane replaces the broken
    state)."""
    if slug not in bottles:
        return (
            f"[{slug}] not dashboard-owned — use ./cli.py cleanup"
        )
    cm, bottle, identity = bottles.pop(slug)

    def _do_teardown() -> None:
        # Best-effort snapshot before teardown so the operator
        # can still inspect the agent's last state via the
        # preserved transcript dir even after explicit stop.
        # exit_code=0 → no auto-preserve; the operator's
        # existing preserve marker (if any) is honored by
        # settle_state below.
        try:
            if getattr(bottle, "agent_provider_template", "claude") == "claude":
                capture_claude_session_state(identity, exit_code=0)
        except BaseException:
            pass
        try:
            cm.__exit__(None, None, None)
        except BaseException:
            pass

    # Mirror the bringup path's stderr → right-pane routing.
    # Reuses any existing right pane (which is probably the
    # agent's own agent session) via `_ensure_right_pane`; the
    # final buffered output stays visible after settle_state
    # removes the state dir (tail-F handles file removal).
    try:
        with _route_op_to_right_pane(
            tmux_state, slug, "teardown.log",
        ) as routed:
            if routed:
                _do_teardown()
    except BaseException:
        pass
    if routed:
        settle_state(identity)
        if tmux_state is not None:
            tmux_state["slug"] = None
        return f"[{slug}] stopped"

    # Non-tmux: compose-down output writes to the dashboard's
    # terminal directly. Drop curses so the lines render cleanly,
    # restore after.
    curses.endwin()
    try:
        _do_teardown()
    finally:
        stdscr.refresh()
    settle_state(identity)
    if tmux_state is not None and tmux_state.get("slug") == slug:
        tmux_state["slug"] = None
    return f"[{slug}] stopped"


# --- tmux split-pane integration (PRD 0021) --------------------------------
#
# When `$TMUX` is set the dashboard lays itself out as the left
# pane of a two-pane window with the operator's currently-selected
# agent in the right pane. First attach creates the right pane via
# `tmux split-window`; subsequent attaches respawn that pane with
# the new agent's agent session. The dashboard remembers the
# pane id + occupant slug in `tmux_state` so the same pane is
# reused across attaches.


def _in_tmux() -> bool:
    """True when the dashboard is running inside a tmux session.
    Tmux sets `$TMUX` to the path of its server socket."""
    return bool(os.environ.get("TMUX"))


def _agent_runtime_args(
    *, resume: bool, remote_control: bool = False, agent_provider_template: str = "claude",
) -> list[str]:
    """The argv the dashboard hands to `bottle.agent_argv`
    on every attach — matches what `attach_agent` builds for the
    foreground handoff so both surfaces produce the same claude
    invocation."""
    runtime = runtime_for(agent_provider_template)
    args = list(runtime.bypass_args)
    if remote_control:
        args.extend(runtime.remote_control_args)
    if resume:
        args.extend(runtime.resume_args)
    return args


def _build_resume_argv_with_fallback(
    bottle, *, remote_control: bool = False, agent_provider_template: str = "claude",
) -> list[str]:
    """Build a backend-exec argv that runs `claude --continue` and
    falls back to plain `claude` if no prior session exists.

    `--continue` exits non-zero when an agent has been spun up
    but never typed at — there's no transcript to resume. The
    shell-level `||` wrapper makes that case start a fresh
    session instead of crashing the pane. The trade-off: we
    invoke `sh -c` inside the bottle, so the command is two
    `claude` invocations behind a tiny shell rather than one
    direct exec. Acceptable; the shell adds microseconds and
    the fallback only kicks in when --continue would have
    failed anyway.

    Works across backends because `bottle.agent_argv` always
    surfaces the `claude` token preceded by the backend's exec
    framing (docker: `docker exec -it <c>`; smolmachines:
    `smolvm machine exec --name <m> -- runuser -u node --`).
    Splitting at `claude` keeps the framing as the prefix and
    wraps just the agent tail in `sh -c`."""
    if agent_provider_template != "claude":
        return bottle.agent_argv(
            _agent_runtime_args(
                resume=True,
                remote_control=remote_control,
                agent_provider_template=agent_provider_template,
            )
        )
    base_args = _agent_runtime_args(
        resume=False,
        remote_control=remote_control,
        agent_provider_template=agent_provider_template,
    )
    base_exec = bottle.agent_argv(base_args)
    # Split exec-framing prefix from the agent-and-args tail so
    # we can compose `<claude…> --continue || <claude…>` inside
    # `sh -c`. The provider command token is the marker.
    command = getattr(bottle, "agent_command", runtime_for(agent_provider_template).command)
    agent_idx = base_exec.index(command)
    prefix = base_exec[:agent_idx]
    agent_cmd = " ".join(shlex.quote(a) for a in base_exec[agent_idx:])
    resume_args = " ".join(
        shlex.quote(a) for a in runtime_for(agent_provider_template).resume_args
    )
    return [
        *prefix,
        "sh", "-c",
        f"{agent_cmd} {resume_args} || {agent_cmd}",
    ]


def _build_split_pane_argv(agent_argv: list[str]) -> list[str]:
    """Pure helper: wrap a backend-exec argv with `tmux split-window
    -h -P -F '#{pane_id}'`. The `-P -F` combo tells tmux to print
    the new pane's id on stdout so we can track it for later
    `respawn-pane` calls."""
    return [
        "tmux", "split-window", "-h",
        "-P", "-F", "#{pane_id}",
        *agent_argv,
    ]


def _build_respawn_pane_argv(pane_id: str, agent_argv: list[str]) -> list[str]:
    """Pure helper: wrap a backend-exec argv with `tmux respawn-pane
    -k -t <pane_id>`. `-k` kills the existing process in the pane
    before respawning."""
    return ["tmux", "respawn-pane", "-k", "-t", pane_id, *agent_argv]


@contextlib.contextmanager
def _redirect_stderr_to_file(path):
    """Redirect file descriptor 2 (stderr) to `path` for the
    duration of the with-block.

    Both Python sys.stderr writes AND subprocess inheritors'
    stderr land in the file because fd 2 is what they share.
    Used by `_new_agent_flow` (PRD 0021 follow-up) to route
    `backend.launch`'s compose-up + provision output into a
    log file the right tmux pane is tailing — so the dashboard
    pane stays uncluttered."""
    log_fd = os.open(
        str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644,
    )
    saved_fd = os.dup(2)
    try:
        sys.stderr.flush()
        os.dup2(log_fd, 2)
        try:
            yield
        finally:
            sys.stderr.flush()
            os.dup2(saved_fd, 2)
    finally:
        os.close(saved_fd)
        os.close(log_fd)


def _tmux_split_pane_create(argv: list[str]) -> str | None:
    """Open a right pane running `argv` via `tmux split-window
    -h`. Returns the new pane's id on success, None on any
    failure (tmux missing, nonzero exit, empty stdout). Generic
    over `argv` so both the tail-during-bringup path and the
    claude-attach path can build on it."""
    try:
        result = subprocess.run(
            _build_split_pane_argv(argv),
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    pane_id = (result.stdout or "").strip()
    return pane_id or None


def _tmux_respawn_pane(pane_id: str, argv: list[str]) -> bool:
    """Replace the content of `pane_id` with `argv` via `tmux
    respawn-pane -k`. Returns True on success. Generic over
    `argv` so the same helper handles tail→claude transitions
    and slug→slug claude transitions."""
    try:
        result = subprocess.run(
            _build_respawn_pane_argv(pane_id, argv),
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


@contextlib.contextmanager
def _route_op_to_right_pane(
    tmux_state: dict | None,
    slug: str,
    log_name: str,
):
    """Run an operation with its stderr routed into the right
    tmux pane via `tail -F`.

    Yields True when routing succeeded — the with-block runs
    with fd 2 redirected to `state/<slug>/<log_name>` and the
    right pane is tailing the same file. Yields False otherwise
    (not in tmux, no tmux_state, or tmux failed to spawn the
    pane) — the caller decides how to fall back.

    Used identically by the bringup flow (log_name='bringup.log')
    and the teardown flow ('teardown.log'). The fallback paths
    differ between callers — bringup follows up with
    `_attach_in_tmux`, teardown does the curses-endwin direct
    compose-down — so the helper stops at "stderr is now routed
    or it isn't" and lets callers branch from there."""
    if not _in_tmux() or tmux_state is None:
        yield False
        return
    log_path = bottle_state_dir(slug) / log_name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("")  # empty so tail starts clean
    pane_id = _ensure_right_pane(tmux_state, ["tail", "-F", str(log_path)])
    if pane_id is None:
        yield False
        return
    tmux_state["slug"] = slug
    with _redirect_stderr_to_file(log_path):
        yield True


def _tmux_close_right_pane(tmux_state: dict) -> None:
    """Close the tracked right pane via `tmux kill-pane`. Clears
    both pane_id and slug in `tmux_state`. Used after the last
    dashboard-owned agent is stopped — no agent session left
    to host, so the pane shouldn't linger."""
    pane_id = tmux_state.get("pane_id")
    if pane_id and _tmux_pane_exists(pane_id):
        try:
            subprocess.run(
                ["tmux", "kill-pane", "-t", pane_id],
                capture_output=True, text=True, check=False,
            )
        except FileNotFoundError:
            pass
    tmux_state["pane_id"] = None
    tmux_state["slug"] = None


def _pick_next_after_stop(
    agents_before: list[ActiveAgent],
    selected_index: int,
    stopped_slug: str,
) -> tuple[int, ActiveAgent] | None:
    """After stopping `stopped_slug` from the agents list, choose
    the agent that should take focus next. The agent below the
    stopped row (which slides up to fill its index) is the
    natural pick; if the stopped agent was last, the row above
    instead. Returns (new_index, agent) or None if no agents
    remain. Pure — easy to unit-test."""
    new_agents = [a for a in agents_before if a.slug != stopped_slug]
    if not new_agents:
        return None
    new_index = min(max(selected_index, 0), len(new_agents) - 1)
    return new_index, new_agents[new_index]


def _ensure_right_pane(tmux_state: dict, argv: list[str]) -> str | None:
    """Run `argv` in the dashboard's right pane — respawn an
    existing tracked pane if one is alive, split-window to
    create one otherwise. Updates `tmux_state['pane_id']` and
    returns the pane id on success, None on failure.

    This is the single place where "respawn or create" lives —
    used by `_attach_in_tmux` for agent sessions AND by
    `_new_agent_flow` for the bringup-log tail. Without this,
    every new-agent start would pile up a fresh right pane
    instead of reusing the one already next to the dashboard."""
    pane_id = tmux_state.get("pane_id")
    if pane_id and _tmux_pane_exists(pane_id):
        if _tmux_respawn_pane(pane_id, argv):
            return pane_id
        # respawn failed — fall through to create a fresh split.
        tmux_state["pane_id"] = None
    new_pane_id = _tmux_split_pane_create(argv)
    if new_pane_id is not None:
        tmux_state["pane_id"] = new_pane_id
    return new_pane_id


def _tmux_pane_exists(pane_id: str) -> bool:
    """True when `pane_id` appears in `tmux list-panes -F
    '#{pane_id}'`. Used before respawn-pane to detect a pane the
    operator manually closed via `C-b x`; an absent pane id means
    we need to create a fresh split."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-F", "#{pane_id}"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return False
    if result.returncode != 0:
        return False
    return pane_id in (result.stdout or "").splitlines()


def _attach_via_handoff(
    stdscr: "curses._CursesWindow",
    bottle,
    slug: str,
    *,
    resume: bool,
) -> str:
    """Foreground handoff: curses.endwin → attach claude → curses
    refresh. The non-tmux path (and the failover from
    `_attach_in_tmux` when tmux misbehaves)."""
    curses.endwin()
    try:
        agent_provider_template = getattr(bottle, "agent_provider_template", "claude")
        exit_code = attach_agent(
            bottle,
            remote_control=False,
            resume=resume,
            agent_provider_template=agent_provider_template,
        )
    except BaseException:
        stdscr.refresh()
        raise
    stdscr.refresh()
    return f"[{slug}] agent session ended (exit {exit_code})"


def _attach_in_tmux(
    stdscr: "curses._CursesWindow",
    bottle,
    slug: str,
    *,
    resume: bool,
    tmux_state: dict,
    focus_right_pane: bool = False,
) -> str:
    """Spawn / respawn the right pane with `bottle`'s claude
    session. Mutates `tmux_state` ({'pane_id': str|None,
    'slug': str|None}) so the main loop can track which slug is
    in the right pane (used by the agents-pane indicator + the
    explicit-stop hook).

    `focus_right_pane=True` runs `tmux select-pane` after the
    respawn so the operator is dropped into agent immediately.
    The Enter re-attach key passes this; passive paths (the
    auto-attach after a stop) leave it False so the operator
    stays in the dashboard pane."""
    if resume:
        agent_provider_template = getattr(bottle, "agent_provider_template", "claude")
        # `--continue` exits non-zero when no prior session
        # exists (agent spun up but never typed at). Wrap with a
        # shell-level fallback so the pane lands in a fresh
        # agent instead of crashing.
        agent_argv = _build_resume_argv_with_fallback(
            bottle, agent_provider_template=agent_provider_template,
        )
    else:
        agent_provider_template = getattr(bottle, "agent_provider_template", "claude")
        agent_argv = bottle.agent_argv(
            _agent_runtime_args(
                resume=False,
                agent_provider_template=agent_provider_template,
            ),
        )
    pane_id = _ensure_right_pane(tmux_state, agent_argv)
    if pane_id is None:
        # tmux failed (missing binary, server died, size error).
        # One status-line failover to the curses handoff so the
        # operator still gets a session.
        return _attach_via_handoff(stdscr, bottle, slug, resume=resume)
    tmux_state["slug"] = slug
    if focus_right_pane:
        _tmux_select_pane(pane_id)
    return f"[{slug}] in right pane"


def _tmux_select_pane(pane_id: str) -> None:
    """`tmux select-pane -t <id>` — moves tmux's keyboard focus
    to the pane. Best-effort; failure is silent (logged only via
    subprocess's stderr, which we suppress)."""
    try:
        subprocess.run(
            ["tmux", "select-pane", "-t", pane_id],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        pass


def _attach_to_bottle(
    stdscr: "curses._CursesWindow",
    bottle,
    slug: str,
    *,
    tmux_state: dict | None = None,
) -> str:
    """Re-attach to a running bottle. Inside tmux (`$TMUX` set +
    `tmux_state` provided) the agent session opens in the
    right pane (created on first attach, respawned on
    subsequent). Outside tmux it's a curses-endwin handoff that
    blocks until the operator exits claude. Re-attach always uses
    `--continue` — first attach happens via `_new_agent_flow`."""
    if _in_tmux() and tmux_state is not None:
        # Enter re-attach is an explicit "I want to interact with
        # this agent" signal — move tmux focus to the right pane
        # so keypresses land in agent instead of the dashboard.
        return _attach_in_tmux(
            stdscr, bottle, slug,
            resume=True, tmux_state=tmux_state,
            focus_right_pane=True,
        )
    return _attach_via_handoff(stdscr, bottle, slug, resume=True)


def _new_agent_flow(
    stdscr: "curses._CursesWindow",
    manifest: Manifest,
    bottles: dict,
    agents_now: list[ActiveAgent],
    tmux_state: dict | None = None,
) -> str:
    """Open the picker, prepare + preflight (modal), launch
    (enter the context manager but DON'T close it), then route
    the first agent session into the right pane (in-tmux) or
    foreground handoff (otherwise). Returns a status-line message
    for the dashboard footer. The (cm, bottle) tuple lands in
    `bottles` keyed by slug; chunk 4 uses it for explicit stop."""
    names = sorted(manifest.agents.keys())
    picked = _picker_modal(stdscr, names, _running_counts(bottles, agents_now))
    if picked is None:
        if not names:
            return "no agents configured; create ~/.bot-bottle/agents/*.md"
        return "agent start aborted"

    # Backend picker (issue #77): operator chooses docker /
    # smolmachines per launch. With only one backend installed
    # the modal short-circuits (no need to ask).
    backend_name = _backend_picker_modal(stdscr, picked)
    if backend_name is None:
        return f"start of {picked!r} aborted at backend select"

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

    stage_dir = Path(tempfile.mkdtemp(prefix="bot-bottle-stage."))
    try:
        plan, identity = prepare_with_preflight(
            spec,
            stage_dir=stage_dir,
            render_preflight=_render,
            prompt_yes=_prompt,
            backend_name=backend_name,
        )
        if plan is None:
            settle_state(identity)
            return f"start of {picked!r} aborted at preflight"

        backend = get_bottle_backend(backend_name)

        # PRD 0021 follow-up: in tmux, route the launch step's
        # stderr (Python info() + subprocess inheritors) into
        # the right pane via tail. On success, fall through to
        # `_attach_in_tmux` which respawns the same pane with
        # claude. On failure, fall through to the curses-endwin
        # handoff so the operator still gets a session.
        try:
            with _route_op_to_right_pane(
                tmux_state, plan.slug, "bringup.log",
            ) as routed:
                if routed:
                    cm = backend.launch(plan)
                    bottle = cm.__enter__()
        except BaseException:
            settle_state(identity)
            raise
        if routed:
            bottles[plan.slug] = (cm, bottle, identity)
            # Move tmux focus to the right pane — the operator
            # just spun this agent up, they want to type at it.
            return _attach_in_tmux(
                stdscr, bottle, plan.slug,
                resume=False, tmux_state=tmux_state,
                focus_right_pane=True,
            )

        # Launch step writes to stderr (image build, network create,
        # compose up). Get out of curses' way for the duration so
        # the lines render cleanly; restore curses immediately after.
        curses.endwin()
        try:
            cm = backend.launch(plan)
            bottle = cm.__enter__()
        except BaseException:
            stdscr.refresh()
            settle_state(identity)
            raise
        bottles[plan.slug] = (cm, bottle, identity)

        # Foreground handoff: the agent owns the terminal until exit,
        # then we restore curses.
        try:
            agent_provider_template = getattr(plan, "agent_provider_template", "claude")
            exit_code = attach_agent(
                bottle,
                remote_control=False,
                agent_provider_template=agent_provider_template,
            )
            if agent_provider_template == "claude":
                capture_claude_session_state(identity, exit_code)
        finally:
            stdscr.refresh()
        return f"[{plan.slug}] agent session ended (exit {exit_code})"
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
    except Die as e:
        # die() printed the reason to stderr, but that happened while
        # curses owned the terminal — the text landed on the alternate
        # screen and was wiped when the terminal was restored. Re-surface
        # it now that we're back on the normal screen.
        if e.message:
            error(e.message)
        else:
            error("dashboard exited on a fatal error (no detail captured).")
        return e.code if isinstance(e.code, int) else 1
    except Exception as e:
        # Any other crash inside the TUI. The traceback would otherwise
        # vanish with the alternate screen, so persist it and tell the
        # operator where to look.
        log_path = _write_crash_log(e)
        error(f"dashboard crashed: {type(e).__name__}: {e}")
        error(f"full traceback written to {log_path}")
        return 1
    return 0


def _write_crash_log(exc: BaseException) -> Path:
    """Persist `exc`'s traceback to a stable file under ~/.bot-bottle/
    and return its path.

    The dashboard runs under curses, so a crash's stderr/traceback is
    painted onto the alternate screen and lost when the terminal is
    restored — this leaves the operator a durable record of *why* it
    died. Best-effort: falls back to a tempfile if the home dir can't
    be written."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    entry = f"=== dashboard crash {stamp} ===\n{body}\n"
    try:
        log_dir = _supervise.bot_bottle_root() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "dashboard-crash.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(entry)
        return path
    except OSError:
        fd, tmp = tempfile.mkstemp(
            prefix="bot-bottle-dashboard-crash-", suffix=".log",
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(entry)
        return Path(tmp)


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


def _quit_without_teardown(bottles: dict) -> None:
    """Exit the dashboard process WITHOUT triggering Python's normal
    cleanup of the `bottles` dict's context managers.

    The dict holds `@contextmanager`-decorated objects whose
    underlying generators have implicit close-on-GC behavior:
    when Python's interpreter shutdown collects them, each
    generator's `finally` block runs, which invokes that bottle's
    teardown (`docker compose down`). PRD 0020 explicitly DOESN'T
    want that — quitting the dashboard should leave running
    bottles running. `os._exit` skips all Python-level cleanup
    (GC, atexit, stdio flush, etc.), so the docker compose
    projects survive the dashboard exit untouched.

    The `bottles` arg is accepted for the explicit
    documentation-of-intent — we're choosing not to close
    these. Curses gets its terminal restored via the explicit
    `endwin` below since `os._exit` doesn't run
    curses.wrapper's finally."""
    del bottles  # nothing to do with it; the os._exit is the point
    curses.endwin()
    os._exit(0)


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
    # Default focus on agents — the dashboard is now primarily an
    # agent-management surface (PRD 0020 + 0021). The operator can
    # Tab to proposals when something queues; until then, j/k go
    # through the agents list.
    focus = PANE_AGENTS
    status_line = ""
    # PRD 0020: bottles spun up from inside this dashboard session.
    # Each entry: slug -> (context-manager, Bottle handle, identity).
    # We hold the context manager so chunk 4's `x` can call __exit__
    # on it; quit (`q`) intentionally does NOT iterate this dict
    # (the user wants quit to leave bottles running).
    bottles: dict[str, tuple] = {}
    # PRD 0021: tmux split-pane state. Empty when not in tmux or
    # before the first attach. Mutated by `_attach_in_tmux` /
    # `_stop_bottle_flow` to track which bottle's session is in
    # the right pane right now.
    tmux_state: dict = {"pane_id": None, "slug": None}
    # Manifest is loaded lazily on first `n` so the dashboard
    # doesn't fail to start in a directory with no manifest (e.g.,
    # when the operator is purely watching pre-existing bottles).
    manifest_cache: list[Manifest | None] = [None]

    def _get_manifest() -> Manifest:
        if manifest_cache[0] is None:
            manifest_cache[0] = Manifest.resolve(USER_CWD, missing_ok=True)
        return manifest_cache[0]
    # A malformed manifest must not take the whole dashboard down — the
    # operator may just be watching running bottles. Degrade to a
    # status-line warning instead. (Any non-config error propagates to
    # cmd_dashboard's crash handler.)
    try:
        _loaded = _get_manifest()
    except ManifestError as e:
        status_line = f"config error: {e}"
    else:
        if not _loaded.bottles and not _loaded.agents:
            status_line = "warning: no bot-bottle config/agents found; new-agent picker is empty"
    # First-tick guard: a brand-new dashboard finds any
    # pre-existing queue entries on its first poll; those
    # shouldn't ring the bell as if they just arrived.
    saw_first_tick = False
    # The dashboard's own tmux pane id (tmux sets `$TMUX_PANE`
    # per-pane). Captured at startup so a new-proposal arrival
    # can `tmux select-pane` back to the dashboard from
    # whatever pane the operator is currently in.
    dashboard_pane_id = os.environ.get("TMUX_PANE", "")
    while True:
        pending = discover_pending()
        if selected >= len(pending):
            selected = max(0, len(pending) - 1)

        agents = discover_active_agents()
        if selected_agent >= len(agents):
            selected_agent = max(0, len(agents) - 1)

        now = time.monotonic()
        live_ids = {qp.proposal.id for qp in pending}
        # Detect proposals we've never seen before. Triggers:
        #   - terminal bell (`curses.beep` → tmux's monitor-bell)
        #   - tmux focus jump to the dashboard pane (so the
        #     operator notices even if they were typing at claude)
        #   - dashboard's internal focus flip to the proposals
        #     pane (so j/k navigates the queued items immediately)
        newly_arrived = live_ids - first_seen.keys()
        if saw_first_tick and newly_arrived:
            try:
                curses.beep()
            except curses.error:
                pass
            if dashboard_pane_id and _in_tmux():
                _tmux_select_pane(dashboard_pane_id)
            focus = PANE_PROPOSALS
            # Land the cursor on the first new proposal so the
            # operator can act immediately. Proposals are sorted
            # by arrival_timestamp ascending; find the lowest
            # index whose id is in `newly_arrived`.
            for i, qp in enumerate(pending):
                if qp.proposal.id in newly_arrived:
                    selected = i
                    break
        for proposal_id in live_ids:
            first_seen.setdefault(proposal_id, now)
        for stale_id in list(first_seen.keys() - live_ids):
            del first_seen[stale_id]
        saw_first_tick = True

        _render(
            stdscr, pending, selected, status_line,
            agents=agents,
            selected_agent=selected_agent,
            focus=focus,
            right_pane_slug=tmux_state.get("slug"),
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
            _quit_without_teardown(bottles)
            return  # unreachable; _quit_without_teardown os._exit's
        if key == 9:  # Tab
            focus = PANE_AGENTS if focus == PANE_PROPOSALS else PANE_PROPOSALS
            continue
        if key == ord("n"):
            # PRD 0020 chunk 2: open the picker, start + attach to
            # the chosen agent, return to the dashboard with the
            # bottle running.
            try:
                manifest = _get_manifest()
            except ManifestError as e:
                status_line = f"config error: {e}"
                continue
            except Exception as e:
                status_line = f"manifest load failed: {e}"
                continue
            status_line = _new_agent_flow(
                stdscr, manifest, bottles, agents, tmux_state=tmux_state,
            )
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
            # (PRD 0020 chunk 3); `x` explicitly stops a
            # dashboard-owned bottle (chunk 4).
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
                    status_line = _attach_to_bottle(
                        stdscr, bottle, target.slug, tmux_state=tmux_state,
                    )
            elif key == ord("x"):
                target = _selected_agent(focus, agents, selected_agent)
                if target is None:
                    status_line = "no agent selected"
                else:
                    status_line = _stop_bottle_flow(
                        stdscr, bottles, target.slug,
                        tmux_state=tmux_state,
                    )
                    # PRD 0021 follow-up: after stop, slide focus
                    # to the next agent in the list (the one that
                    # filled the stopped row) and respawn the
                    # right pane with its agent session. If
                    # nothing's left, close the right pane.
                    pick = _pick_next_after_stop(
                        agents, selected_agent, target.slug,
                    )
                    if pick is None:
                        _tmux_close_right_pane(tmux_state)
                    else:
                        new_index, next_agent = pick
                        selected_agent = new_index
                        if _in_tmux():
                            manifest = manifest_cache[0]
                            bottle, _hint = _bottle_for_slug(
                                next_agent.slug, bottles, manifest,
                            )
                            _attach_in_tmux(
                                stdscr, bottle, next_agent.slug,
                                resume=True, tmux_state=tmux_state,
                            )
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
    right_pane_slug: str | None = None,
    first_seen: dict[str, float] | None = None,
    now: float | None = None,
    green_attr: int = 0,
) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    agents = agents or []
    header = (
        f"bot-bottle dashboard  "
        f"({len(pending)} pending, {len(agents)} active)"
    )
    stdscr.addnstr(0, 0, header, w - 1, curses.A_BOLD)
    stdscr.hline(1, 0, curses.ACS_HLINE, w)

    proposals_focused = focus == PANE_PROPOSALS
    agents_focused = focus == PANE_AGENTS

    # ----- proposals pane (top) -----
    row = 2
    # When any proposal is in the recent-arrival window (the
    # individual rows are green-highlighted by the existing logic),
    # also highlight the pane label so the alert is visible at a
    # glance even when the operator is focused elsewhere.
    proposals_have_recent = any(
        _is_recent(qp.proposal.id, first_seen, now) for qp in pending
    )
    proposals_label = "proposals:"
    if proposals_have_recent:
        proposals_label += "  (new!)"
    if proposals_focused:
        proposals_label += "  (focused)"
    label_attr = curses.A_DIM
    if proposals_have_recent:
        label_attr = curses.A_BOLD | green_attr
    stdscr.addnstr(row, 0, proposals_label, w - 1, label_attr)
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
            in_right_pane = (a.slug == right_pane_slug)
            if agents_focused and i == selected_agent:
                # Replace the leading "  " cursor with "> " and
                # highlight the whole row.
                line = "> " + line[2:]
                attr = curses.A_REVERSE
            elif in_right_pane:
                # PRD 0021: `*` marks the agent currently in the
                # right tmux pane so the operator can see at a
                # glance which session is visible to their right.
                line = "* " + line[2:]
                attr = curses.A_BOLD
            else:
                attr = curses.A_NORMAL
            stdscr.addnstr(row, 0, line, w - 1, attr)
            row += 1

    footer = (
        "[n] new  [Tab] switch  [j/k] move  "
        "[Enter] view/attach  [x] stop  [a/m/r] proposal  [e/p] edit  [q] quit"
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
    """One-line agent row: `  [<backend>] <slug> <agent_name> started
    <HH:MM:SS> [<sidecars>]`. The `agent` service is filtered out of
    the displayed list — it's always present for an active bottle,
    so listing it carries no information; the sidecars are the
    differentiator.

    The `[docker]` / `[smolmachines]` prefix lets the operator tell
    which backend a bottle came from (issue #77). Truncated to
    `maxw` because the renderer's addnstr only enforces width if
    we hand it a properly-sized string."""
    started = (
        a.started_at.split("T", 1)[1][:8]
        if "T" in a.started_at else (a.started_at or "?")
    )
    sidecars = tuple(s for s in a.services if s != "agent")
    services = ",".join(sidecars) if sidecars else "(starting)"
    backend_tag = f"[{a.backend_name}]" if a.backend_name else ""
    line = (
        f"  {backend_tag} {a.slug}  {a.agent_name}  "
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
