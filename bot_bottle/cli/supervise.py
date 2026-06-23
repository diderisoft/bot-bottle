"""supervise: list pending supervise proposals across all bottles and
act on them (approve / modify / reject).

Curses-based TUI; modify-then-approve shells out to $EDITOR. The
approval handler wires to PRD 0016 (capability-block), which rebuilds
the bottle Dockerfile. Egress proposals are queued for operator review
as full routes.yaml updates.
"""

from __future__ import annotations

import argparse
import curses
import os
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import supervise as _supervise
from ..bottle_state import read_metadata
# from ..backend.docker.capability_apply import (
#     CapabilityApplyError,
#     apply_capability_change,
# )
from ..backend.docker.egress_apply import (
    EgressApplyError,
    applicator as _docker_applicator,
)
from ..backend.macos_container.egress_apply import (
    applicator as _macos_applicator,
)
from ..backend.smolmachines.egress_apply import (
    applicator as _smolmachines_applicator,
)
from ..log import Die, error, info


class CapabilityApplyError(RuntimeError):
    """Placeholder while capability_apply is disabled."""

from ..supervise import (
    COMPONENT_FOR_TOOL,
    AuditEntry,
    Proposal,
    Response,
    STATUS_APPROVED,
    STATUS_MODIFIED,
    STATUS_REJECTED,
    TOOL_CAPABILITY_BLOCK,
    TOOL_ALLOW,
    TOOL_EGRESS_BLOCK,
    TOOL_GITLEAKS_ALLOW,
    archive_proposal,
    list_pending_proposals,
    render_diff,
    write_audit_entry,
    write_response,
)
from ._common import PROG


_REFRESH_INTERVAL_MS = 1000


@dataclass(frozen=True)
class QueuedProposal:
    """A pending proposal plus the queue dir it was found in."""

    proposal: Proposal
    queue_dir: Path


# Errors any remediation engine may raise. Caught by the TUI key
# handlers and surfaced in the status line so a failed apply keeps
# the proposal pending rather than crashing curses.
ApplyError = (CapabilityApplyError, EgressApplyError)


def apply_routes_change(slug: str, content: str) -> tuple[str, str]:
    meta = read_metadata(slug)
    backend = meta.backend if meta is not None else ""
    if backend == "macos-container":
        return _macos_applicator.apply_routes_change(slug, content)
    if backend == "smolmachines":
        return _smolmachines_applicator.apply_routes_change(slug, content)
    return _docker_applicator.apply_routes_change(slug, content)


def discover_pending() -> list[QueuedProposal]:
    """Walk ~/.bot-bottle/queue/* and collect pending proposals."""
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


def _approval_status(qp: QueuedProposal, verb: str) -> str:
    """Status-line text after a successful approval."""
    base = f"{verb} {qp.proposal.tool} for [{qp.proposal.bottle_slug}]"
    return f"{base}; resume: ./cli.py resume {qp.proposal.bottle_slug}"


def _detail_lines(
    qp: QueuedProposal,
    *,
    green_attr: int = 0,
) -> list[tuple[str, int]]:
    """Return the detail-view body as (text, curses-attr) tuples."""
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
        ("proposed file:", 0),
    ])
    out.extend((line, 0) for line in p.proposed_file.splitlines() or [""])
    return out


def _suffix_for_tool(tool: str) -> str:
    if tool == TOOL_CAPABILITY_BLOCK:
        return ".dockerfile"
    if tool in (TOOL_ALLOW, TOOL_EGRESS_BLOCK):
        return ".yaml"
    if tool == TOOL_GITLEAKS_ALLOW:
        return ".txt"
    return ".txt"


# --- Operator actions ------------------------------------------------------


def approve(
    qp: QueuedProposal,
    *,
    notes: str = "",
    final_file: str | None = None,
) -> None:
    """Apply the proposal, write the waiting response, and audit it."""
    status = STATUS_MODIFIED if final_file is not None else STATUS_APPROVED
    file_to_apply = final_file if final_file is not None else qp.proposal.proposed_file

    diff_before, diff_after = "", ""
    # if qp.proposal.tool == TOOL_CAPABILITY_BLOCK:
    #     _meta = read_metadata(qp.proposal.bottle_slug)
    #     if _meta is not None and not _meta.compose_project:
    #         raise CapabilityApplyError(
    #             "capability-block remediation is not supported for smolmachines "
    #             "bottles. Reject this proposal or handle the capability change "
    #             "manually, then restart the bottle."
    #         )
    #     diff_before, diff_after = apply_capability_change(
    #         qp.proposal.bottle_slug, file_to_apply,
    #     )
    if qp.proposal.tool in (TOOL_ALLOW, TOOL_EGRESS_BLOCK):
        diff_before, diff_after = apply_routes_change(
            qp.proposal.bottle_slug,
            file_to_apply,
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
        archive_proposal(qp.queue_dir, qp.proposal.id)


def reject(qp: QueuedProposal, *, reason: str) -> None:
    """Write a rejection response and an audit entry."""
    response = Response(
        proposal_id=qp.proposal.id,
        status=STATUS_REJECTED,
        notes=reason,
        final_file=None,
    )
    write_response(qp.queue_dir, response)
    _write_audit(qp, action=STATUS_REJECTED, notes=reason, diff_before="", diff_after="")


def _approve_from_tui(
    stdscr: "curses._CursesWindow",  # type: ignore
    qp: QueuedProposal,
    *,
    final_file: str | None = None,
    notes: str = "",
) -> str:
    """Approve from curses, prompting for any tool-specific audit note."""
    if qp.proposal.tool == TOOL_GITLEAKS_ALLOW and final_file is None:
        notes = _prompt(stdscr, "allow reason (test fixture/false positive): ")
        if not notes:
            return "approve aborted (empty reason)"
    approve(qp, final_file=final_file, notes=notes)
    verb = "modified+approved" if final_file is not None else "approved"
    return _approval_status(qp, verb)


def _write_audit(
    qp: QueuedProposal,
    *,
    action: str,
    notes: str,
    diff_before: str,
    diff_after: str,
) -> None:
    """Audit log for egress tool."""
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
    """Open `content` in $EDITOR and return edited content, if changed."""
    editor = os.environ.get("EDITOR", "vim")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, prefix="supervise-modify.",
    ) as f:
        f.write(content)
        path = f.name
    try:
        subprocess.run([editor, path], check=False)
        with open(path, encoding="utf-8") as f:
            edited = f.read()
        return edited if edited != content else None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# --- TUI -------------------------------------------------------------------


def cmd_supervise(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} supervise", add_help=True)
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
        if e.message:
            error(e.message)
        else:
            error("supervise exited on a fatal error (no detail captured).")
        return e.code if isinstance(e.code, int) else 1
    except Exception as e:  # noqa: W0718 — catch supervise crash for logging
        log_path = _write_crash_log(e)
        error(f"supervise crashed: {type(e).__name__}: {e}")
        error(f"full traceback written to {log_path}")
        return 1
    return 0


def _write_crash_log(exc: BaseException) -> Path:
    """Persist `exc`'s traceback to a stable file under ~/.bot-bottle/."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    entry = f"=== supervise crash {stamp} ===\n{body}\n"
    try:
        log_dir = _supervise.bot_bottle_root() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "supervise-crash.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(entry)
        return path
    except OSError:
        fd, tmp = tempfile.mkstemp(
            prefix="bot-bottle-supervise-crash-", suffix=".log",
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


def _try_init_green() -> int:
    """Initialise a green color pair and return its attr, or 0."""
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        return curses.color_pair(1)
    except curses.error:
        return 0


def _main_loop(stdscr: "curses._CursesWindow") -> None:  # type: ignore
    curses.curs_set(0)
    stdscr.timeout(_REFRESH_INTERVAL_MS)
    green_attr = _try_init_green()
    selected = 0
    status_line = ""
    seen_ids: set[str] = set()

    while True:
        pending = discover_pending()
        if selected >= len(pending):
            selected = max(0, len(pending) - 1)

        live_ids = {qp.proposal.id for qp in pending}
        newly_arrived = live_ids - seen_ids
        if seen_ids and newly_arrived:
            try:
                curses.beep()
            except curses.error:
                pass
            for i, qp in enumerate(pending):
                if qp.proposal.id in newly_arrived:
                    selected = i
                    break
        seen_ids = live_ids

        _render(
            stdscr, pending, selected, status_line,
            green_attr=green_attr,
        )

        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return

        if key == -1:
            continue

        status_line = ""

        if key in (ord("q"), 27):
            return

        if not pending:
            continue
        qp = pending[selected]

        if key in (curses.KEY_DOWN, ord("j")):
            selected = min(selected + 1, len(pending) - 1)
        elif key in (curses.KEY_UP, ord("k")):
            selected = max(selected - 1, 0)
        elif key in (curses.KEY_ENTER, 10, 13):
            _detail_view(stdscr, qp, green_attr=green_attr)
        elif key == ord("a"):
            try:
                status_line = _approve_from_tui(stdscr, qp)
            except ApplyError as e:
                status_line = f"apply failed: {e}"
        elif key == ord("m"):
            if qp.proposal.tool == TOOL_GITLEAKS_ALLOW:
                status_line = "modify unavailable for gitleaks-allow"
                continue
            edited = _modify(stdscr, qp)
            if edited is None:
                status_line = "modify aborted (no change)"
            else:
                try:
                    status_line = _approve_from_tui(
                        stdscr, qp, final_file=edited,
                        notes="operator modified before approving",
                    )
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
    stdscr: "curses._CursesWindow",  # type: ignore
    pending: list[QueuedProposal],
    selected: int,
    status_line: str,
    *,
    green_attr: int = 0,  # noqa: F841 — unused, but required by interface
) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    header = f"bot-bottle supervise  ({len(pending)} pending)"
    stdscr.addnstr(0, 0, header, w - 1, curses.A_BOLD)
    stdscr.hline(1, 0, curses.ACS_HLINE, w)

    row = 2
    if not pending:
        stdscr.addnstr(
            row, 2,
            "no pending proposals; agents will queue here when they call a "
            "supervise tool",
            w - 4,
        )
    else:
        for i, qp in enumerate(pending):
            if row >= h - 3:
                break
            p = qp.proposal
            ts_short = (
                p.arrival_timestamp.split("T", 1)[1][:8]
                if "T" in p.arrival_timestamp else p.arrival_timestamp
            )
            cursor = "> " if i == selected else "  "
            line = (
                f"{cursor}{ts_short}  "
                f"[{p.bottle_slug}]  {p.tool:<18}  {p.id[:8]}"
            )
            attr = curses.A_REVERSE if i == selected else curses.A_NORMAL
            stdscr.addnstr(row, 0, line, w - 1, attr)
            row += 1
            if row >= h - 3:
                break
            if p.justification:
                stdscr.addnstr(row, 4, p.justification[: max(0, w - 5)], w - 5)
                row += 1

    footer = "[j/k] move  [Enter] view  [a] approve  [m] modify  [r] reject  [q] quit"
    stdscr.hline(h - 2, 0, curses.ACS_HLINE, w)
    stdscr.addnstr(h - 1, 0, footer, w - 1, curses.A_DIM)
    if status_line:
        stdscr.addnstr(h - 3, 0, status_line, w - 1, curses.A_BOLD)
    stdscr.refresh()


def _detail_view(
    stdscr: "curses._CursesWindow",  # type: ignore
    qp: QueuedProposal,
    *,
    green_attr: int = 0,
) -> None:
    """Render the full proposal. Scrollable. Press q to return."""
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
                _approve_from_tui(stdscr, qp)
            except ApplyError:
                pass
            return
        elif key == ord("m"):
            if qp.proposal.tool == TOOL_GITLEAKS_ALLOW:
                return
            edited = _modify(stdscr, qp)
            if edited is not None:
                try:
                    _approve_from_tui(
                        stdscr, qp, final_file=edited,
                        notes="operator modified before approving",
                    )
                except ApplyError:
                    pass
            return
        elif key == ord("r"):
            reason = _prompt(stdscr, "reject reason: ")
            if reason:
                reject(qp, reason=reason)
            return


def _modify(stdscr: "curses._CursesWindow", qp: QueuedProposal) -> str | None:  # type: ignore
    """Suspend curses, open $EDITOR on the proposed file, return edited content."""
    suffix = _suffix_for_tool(qp.proposal.tool)
    curses.endwin()
    try:
        edited = edit_in_editor(qp.proposal.proposed_file, suffix=suffix)
    finally:
        stdscr.refresh()
    return edited


def _prompt(stdscr: "curses._CursesWindow", label: str) -> str:  # type: ignore
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
    "QueuedProposal",
    "approve",
    "cmd_supervise",
    "discover_pending",
    "edit_in_editor",
    "reject",
]
