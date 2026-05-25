"""dashboard: list pending supervise proposals across all bottles and
act on them (approve / modify / reject). PRD 0013 v1.

Curses-based TUI; modify-then-approve shells out to $EDITOR. For
0013 the approval handlers are no-ops on the supervisor side: the
response file is written (and the sidecar returns it to the agent),
and an audit entry is appended, but no host-side config change runs.
PRDs 0014 (cred-proxy) and 0015 (pipelock) wire in the actual
writes.
"""

from __future__ import annotations

import argparse
import curses
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import supervise as _supervise
from ..backend.docker.capability_apply import (
    CapabilityApplyError,
    apply_capability_change,
)
from ..backend.docker.cred_proxy_apply import (
    CredProxyApplyError,
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
    TOOL_CRED_PROXY_BLOCK,
    TOOL_PIPELOCK_BLOCK,
    archive_proposal,
    list_pending_proposals,
    render_diff,
    write_audit_entry,
    write_response,
)
from ._common import PROG


# Errors any remediation engine may raise. Caught by the TUI key
# handlers and surfaced in the status line so a failed apply keeps
# the proposal pending rather than crashing curses.
ApplyError = (CredProxyApplyError, PipelockApplyError, CapabilityApplyError)


# --- Discovery -------------------------------------------------------------


@dataclass(frozen=True)
class QueuedProposal:
    """A pending proposal plus the queue dir it was found in."""

    proposal: Proposal
    queue_dir: Path


def _discover_sidecar_slugs(name_prefix: str) -> list[str]:
    """Slugs of bottles whose sidecar container names start with
    `name_prefix`. Empty list if docker isn't reachable or not
    installed."""
    try:
        r = subprocess.run(
            [
                "docker", "ps",
                "--filter", f"name=^{name_prefix}",
                "--format", "{{.Names}}",
            ],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return []
    if r.returncode != 0:
        return []
    out: list[str] = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line.startswith(name_prefix):
            out.append(line[len(name_prefix):])
    return sorted(out)


def discover_cred_proxy_slugs() -> list[str]:
    """Slugs of bottles with a running cred-proxy sidecar. Used by
    the operator-initiated `routes edit` verb."""
    return _discover_sidecar_slugs("claude-bottle-cred-proxy-")


def discover_pipelock_slugs() -> list[str]:
    """Slugs of bottles with a running pipelock sidecar. Used by
    the operator-initiated `pipelock edit` verb."""
    return _discover_sidecar_slugs("claude-bottle-pipelock-")


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

    Raises CredProxyApplyError if the cred-proxy-block apply fails
    (sidecar down, invalid JSON survived the operator's modify).
    On failure no response is written and no audit entry is
    appended — the proposal stays pending so the operator can fix
    the input and retry."""
    status = STATUS_MODIFIED if final_file is not None else STATUS_APPROVED
    file_to_apply = final_file if final_file is not None else qp.proposal.proposed_file

    diff_before, diff_after = "", ""
    if qp.proposal.tool == TOOL_CRED_PROXY_BLOCK:
        diff_before, diff_after = apply_routes_change(
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
    """Apply an operator-initiated routes.json change (no agent
    proposal). Used by the `routes edit <bottle>` TUI verb and
    available for scripted use. Returns (before, after) like
    apply_routes_change. Writes an audit entry tagged
    ACTION_OPERATOR_EDIT to distinguish from tool-call approvals.

    Raises CredProxyApplyError on failure."""
    before, after = apply_routes_change(slug, new_content)
    write_audit_entry(AuditEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        bottle_slug=slug,
        component="cred-proxy",
        operator_action=ACTION_OPERATOR_EDIT,
        operator_notes="",
        justification="",
        diff=render_diff(before, after, label="cred-proxy"),
    ))
    return before, after


def _apply_pipelock_url(slug: str, failed_url: str) -> tuple[str, str]:
    """pipelock-block proposals carry a single failed URL, not a
    full allowlist. Extract the host, merge into the running
    allowlist, and hand the merged content to apply_allowlist_change.
    The full URL (with path) is preserved on the proposal for the
    operator's read; only the host ends up in pipelock's allowlist.

    FOLLOW-UP — path-aware filtering. Pipelock 2.3.0's api_allowlist
    is hostname-only (verified by inspecting the binary's strict
    preset; the only "path" fields in pipelock's schema are about
    local filesystem paths under sandbox / file_sentry / taint). So
    approving pipelock-block opens the entire host, not the URL's
    path. If/when per-path enforcement becomes load-bearing, the
    follow-up is most likely adding an `auth_scheme: none` mode +
    `path_allowlist` field to cred-proxy (which already does
    path-prefix routing) and rewiring pipelock-block to propose
    cred-proxy routes instead of pipelock hostnames. That's a
    multi-touch change deserving its own PRD — out of scope for the
    supervise-loop work that introduced this function. See PR
    discussion on https://gitea.dideric.is/didericis/claude-bottle/pulls/25
    for the design conversation."""
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
    """Audit log for cred-proxy / pipelock tools. capability-block has
    no audit log (its changes are captured by the bottle's rebuild
    record + git history per PRD 0016).

    For cred-proxy-block approvals the (before, after) come from the
    apply_routes_change return — a real fetched-from-sidecar diff.
    For rejections, or for tools whose remediation hasn't landed yet
    (pipelock in 0014, capability anywhere), both are empty strings
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
    status_line = ""
    while True:
        pending = discover_pending()
        if selected >= len(pending):
            selected = max(0, len(pending) - 1)

        now = time.monotonic()
        live_ids = {qp.proposal.id for qp in pending}
        for proposal_id in live_ids:
            first_seen.setdefault(proposal_id, now)
        for stale_id in list(first_seen.keys() - live_ids):
            del first_seen[stale_id]

        _render(stdscr, pending, selected, status_line, first_seen, now, green_attr)

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
        if key == ord("e"):
            status_line = _operator_edit_routes_flow(stdscr)
            continue
        if key == ord("p"):
            status_line = _operator_edit_allowlist_flow(stdscr)
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
    first_seen: dict[str, float] | None = None,
    now: float | None = None,
    green_attr: int = 0,
) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    header = f"claude-bottle dashboard  ({len(pending)} pending)"
    stdscr.addnstr(0, 0, header, w - 1, curses.A_BOLD)
    stdscr.hline(1, 0, curses.ACS_HLINE, w)

    if not pending:
        stdscr.addnstr(
            3, 2,
            "no pending proposals; agents will queue here when they call a "
            "supervise tool",
            w - 4,
        )
    else:
        for i, qp in enumerate(pending):
            row = 2 + i
            if row >= h - 2:
                break
            p = qp.proposal
            ts_short = p.arrival_timestamp.split("T", 1)[1][:8] if "T" in p.arrival_timestamp else p.arrival_timestamp
            line = (
                f"{'> ' if i == selected else '  '}"
                f"[{p.bottle_slug}] {p.tool:<20} {ts_short}  "
                f"{p.justification[:60]}"
            )
            attr = curses.A_REVERSE if i == selected else curses.A_NORMAL
            if _is_recent(p.id, first_seen, now):
                attr |= green_attr
            stdscr.addnstr(row, 0, line, w - 1, attr)

    footer = (
        "[Enter] view  [a] approve  [m] modify  [r] reject  "
        "[e] routes edit  [p] pipelock edit  [j/k] move  [q] quit"
    )
    stdscr.hline(h - 2, 0, curses.ACS_HLINE, w)
    stdscr.addnstr(h - 1, 0, footer, w - 1, curses.A_DIM)
    if status_line:
        stdscr.addnstr(h - 3, 0, status_line, w - 1, curses.A_BOLD)
    stdscr.refresh()


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
            out.append(("", 0))
            out.append((f"→ would allow host: {host}", green_attr))
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
    # cred-proxy-block / pipelock-block: JSON-ish + plain.
    return ".txt"


def _operator_edit_routes_flow(stdscr: "curses._CursesWindow") -> str:
    """Operator-initiated routes.json edit. Discover running
    cred-proxy sidecars, pick one (single → use directly; multi →
    prompt), fetch the current routes, open in $EDITOR, apply on
    save. Returns a status-line message."""
    return _operator_edit_flow(
        stdscr,
        label="routes",
        discover=discover_cred_proxy_slugs,
        fetch=fetch_current_routes,
        apply=operator_edit_routes,
        suffix=".json",
    )


def _operator_edit_allowlist_flow(stdscr: "curses._CursesWindow") -> str:
    """Operator-initiated pipelock allowlist edit."""
    return _operator_edit_flow(
        stdscr,
        label="pipelock",
        discover=discover_pipelock_slugs,
        fetch=fetch_current_allowlist,
        apply=operator_edit_allowlist,
        suffix=".txt",
    )


def _operator_edit_flow(
    stdscr: "curses._CursesWindow",
    *,
    label: str,
    discover,
    fetch,
    apply,
    suffix: str,
) -> str:
    """Shared scaffolding for the routes-edit + pipelock-edit verbs.
    `discover` returns running-sidecar slugs; `fetch(slug)` returns
    the current operator-facing config; `apply(slug, new)` does the
    write + restart/SIGHUP and writes the audit entry."""
    slugs = discover()
    if not slugs:
        return f"no running {label} sidecars to edit"
    if len(slugs) == 1:
        slug = slugs[0]
    else:
        slug = _prompt(stdscr, f"bottle ({', '.join(slugs)}): ")
        if not slug:
            return f"{label} edit aborted"
        if slug not in slugs:
            return f"unknown bottle {slug!r}"
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
