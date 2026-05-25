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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import supervise as _supervise
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
    list_pending_proposals,
    render_diff,
    write_audit_entry,
    write_response,
)
from ._common import PROG


# --- Discovery -------------------------------------------------------------


@dataclass(frozen=True)
class QueuedProposal:
    """A pending proposal plus the queue dir it was found in."""

    proposal: Proposal
    queue_dir: Path


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
    """Write an approval response and an audit entry. If `final_file`
    is provided the status is `modified`; otherwise `approved`."""
    status = STATUS_MODIFIED if final_file is not None else STATUS_APPROVED
    response = Response(
        proposal_id=qp.proposal.id,
        status=status,
        notes=notes,
        final_file=final_file,
    )
    write_response(qp.queue_dir, response)
    _write_audit(qp, action=status, notes=notes, final_file=final_file)


def reject(qp: QueuedProposal, *, reason: str) -> None:
    """Write a rejection response and an audit entry."""
    response = Response(
        proposal_id=qp.proposal.id,
        status=STATUS_REJECTED,
        notes=reason,
        final_file=None,
    )
    write_response(qp.queue_dir, response)
    _write_audit(qp, action=STATUS_REJECTED, notes=reason, final_file=None)


def _write_audit(
    qp: QueuedProposal,
    *,
    action: str,
    notes: str,
    final_file: str | None,
) -> None:
    """Audit log for cred-proxy / pipelock tools. capability-block has
    no audit log (its changes are captured by the bottle's rebuild
    record + git history per PRD 0016)."""
    component = COMPONENT_FOR_TOOL.get(qp.proposal.tool)
    if component is None:
        # capability-block: skip audit log; 0016 records via rebuild.
        return
    # v1 audit diff is empty: 0013's no-op handler doesn't have the
    # actual current-on-disk file to diff against, only the agent's
    # proposed file. 0014 / 0015 fill in the real diff against the
    # live routes.json / allowlist after writing the change.
    write_audit_entry(AuditEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        bottle_slug=qp.proposal.bottle_slug,
        component=component,
        operator_action=action,
        operator_notes=notes,
        justification=qp.proposal.justification,
        diff=render_diff(
            "",
            final_file if final_file is not None else qp.proposal.proposed_file,
            label=component,
        ),
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


def _main_loop(stdscr: "curses._CursesWindow") -> None:
    curses.curs_set(0)
    stdscr.nodelay(False)
    selected = 0
    status_line = ""
    while True:
        pending = discover_pending()
        if selected >= len(pending):
            selected = max(0, len(pending) - 1)

        _render(stdscr, pending, selected, status_line)
        status_line = ""

        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return

        if key in (ord("q"), 27):  # q or ESC
            return
        if not pending:
            continue
        qp = pending[selected]

        if key in (curses.KEY_DOWN, ord("j")):
            selected = min(selected + 1, len(pending) - 1)
        elif key in (curses.KEY_UP, ord("k")):
            selected = max(selected - 1, 0)
        elif key in (curses.KEY_ENTER, 10, 13, ord("v")):
            _detail_view(stdscr, qp)
        elif key == ord("a"):
            approve(qp)
            status_line = f"approved {qp.proposal.tool} for [{qp.proposal.bottle_slug}]"
        elif key == ord("m"):
            edited = _modify(stdscr, qp)
            if edited is None:
                status_line = "modify aborted (no change)"
            else:
                approve(qp, final_file=edited, notes="operator modified before approving")
                status_line = f"modified+approved {qp.proposal.tool} for [{qp.proposal.bottle_slug}]"
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
            stdscr.addnstr(row, 0, line, w - 1, attr)

    footer = "[Enter] view  [a] approve  [m] modify  [r] reject  [j/k] move  [q] quit"
    stdscr.hline(h - 2, 0, curses.ACS_HLINE, w)
    stdscr.addnstr(h - 1, 0, footer, w - 1, curses.A_DIM)
    if status_line:
        stdscr.addnstr(h - 3, 0, status_line, w - 1, curses.A_BOLD)
    stdscr.refresh()


def _detail_view(stdscr: "curses._CursesWindow", qp: QueuedProposal) -> None:
    """Render the full proposal: header, justification, proposed file
    contents. Scrollable. Press q to return."""
    lines = _detail_lines(qp)
    offset = 0
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        for i, line in enumerate(lines[offset:offset + h - 1]):
            stdscr.addnstr(i, 0, line, w - 1)
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
            approve(qp)
            return
        elif key == ord("m"):
            edited = _modify(stdscr, qp)
            if edited is not None:
                approve(qp, final_file=edited, notes="operator modified before approving")
            return
        elif key == ord("r"):
            reason = _prompt(stdscr, "reject reason: ")
            if reason:
                reject(qp, reason=reason)
            return


def _detail_lines(qp: QueuedProposal) -> list[str]:
    p = qp.proposal
    out = [
        f"bottle: {p.bottle_slug}",
        f"tool: {p.tool}",
        f"id: {p.id}",
        f"arrived: {p.arrival_timestamp}",
        f"queue: {qp.queue_dir}",
        "",
        "justification:",
    ]
    out.extend("  " + line for line in p.justification.splitlines() or [""])
    out.extend([
        "",
        "proposed file:",
    ])
    out.extend(p.proposed_file.splitlines() or [""])
    return out


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
