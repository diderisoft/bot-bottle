"""supervise: a curses TUI listing pending supervise proposals across bottles,
to approve, modify (in $EDITOR) or reject.
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

from ...paths import bot_bottle_root
from ...lib.log import Die, error, info
from ...control.client import (
    OrchestratorClient,
    OrchestratorClientError,
    discover_orchestrator_url,
)

from ...contract.supervise import (
    Proposal,
    TOOL_EGRESS_ALLOW,
    TOOL_EGRESS_BLOCK,
    TOOL_GITLEAKS_ALLOW,
    TOOL_EGRESS_TOKEN_ALLOW,
)
from ..constants import PROG
from ...control.hostctl.orchestrator import ensure_via_daemon
from ..settings import settings_from_env


_REFRESH_INTERVAL_MS = 1000

# Tools whose payload is a read-only report: no modify, and approval needs a
# recorded reason.
_REPORT_ONLY_TOOLS: tuple[str, ...] = (TOOL_GITLEAKS_ALLOW, TOOL_EGRESS_TOKEN_ALLOW)


@dataclass(frozen=True)
class QueuedProposal:
    """A pending proposal. `label` is the bottle's display name;
    `proposal.bottle_slug` is the bottle_id actions are keyed by."""

    proposal: Proposal
    label: str = ""


# Action failures are shown in the status line and leave the proposal pending.
ApplyError = (OrchestratorClientError,)


# The orchestrator, discovered on first use. Every action goes through its HTTP
# control plane.
_client_instance: OrchestratorClient | None = None


def _resolve_orchestrator_url() -> str:
    """The orchestrator URL, starting the backend's infra pair if none is
    running (supervise is often the first thing run)."""
    settings = settings_from_env()
    try:
        return discover_orchestrator_url(settings)
    except OrchestratorClientError:
        from ...backend import get_bottle_backend
        backend = get_bottle_backend(settings)
        info(f"no orchestrator control plane running; starting one ({backend.name})…")
        return ensure_via_daemon(settings, backend.name)


def _client() -> OrchestratorClient:
    global _client_instance  # noqa: PLW0603 — CLI-session singleton
    if _client_instance is None:
        _client_instance = OrchestratorClient(_resolve_orchestrator_url())
    return _client_instance


def discover_pending() -> list[QueuedProposal]:
    """Pending proposals across bottles, from the orchestrator."""
    out = [
        QueuedProposal(
            proposal=Proposal.from_dict(d),
            label=str(d.get("bottle_label") or d.get("bottle_slug") or ""),
        )
        for d in _client().supervise_pending()
    ]
    out.sort(key=lambda q: q.proposal.arrival_timestamp)
    return out


def _approval_status(qp: QueuedProposal, verb: str) -> str:
    """Status-line text after a successful approval."""
    base = f"{verb} {qp.proposal.tool} for [{qp.label}]"
    return f"{base}; resume: bot-bottle resume {qp.label}"


def _detail_lines(
    qp: QueuedProposal,
    *,
    green_attr: int = 0,
) -> list[tuple[str, int]]:
    """The detail view as (text, curses attr) tuples."""
    p = qp.proposal
    out: list[tuple[str, int]] = [
        (f"bottle: {qp.label}", 0),
        (f"tool: {p.tool}", 0),
        (f"id: {p.id}", 0),
        (f"arrived: {p.arrival_timestamp}", 0),
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
    if tool in (TOOL_EGRESS_ALLOW, TOOL_EGRESS_BLOCK):
        return ".yaml"
    if tool in (TOOL_GITLEAKS_ALLOW, TOOL_EGRESS_TOKEN_ALLOW):
        return ".txt"
    return ".txt"


# --- Operator actions ---


def approve(
    qp: QueuedProposal,
    *,
    notes: str = "",
    final_file: str | None = None,
) -> None:
    """Approve, or modify-then-approve with `final_file`. The orchestrator
    applies the change, unblocks the agent and audits it in one operation.
    Raises `OrchestratorClientError`."""
    _client().supervise_respond(
        qp.proposal.id,
        bottle_slug=qp.proposal.bottle_slug,
        decision="modify" if final_file is not None else "approve",
        notes=notes,
        final_file=final_file,
    )


def reject(qp: QueuedProposal, *, reason: str) -> None:
    """Reject via the orchestrator."""
    _client().supervise_respond(
        qp.proposal.id,
        bottle_slug=qp.proposal.bottle_slug,
        decision="reject",
        notes=reason,
    )


def _approve_from_tui(
    stdscr: "curses._CursesWindow",  # type: ignore
    qp: QueuedProposal,
    *,
    final_file: str | None = None,
    notes: str = "",
) -> str:
    """Approve from curses, prompting for any required audit note."""
    if qp.proposal.tool in _REPORT_ONLY_TOOLS and final_file is None:
        notes = _prompt(stdscr, "allow reason (false positive / legitimately needed): ")
        if not notes:
            return "approve aborted (empty reason)"
    approve(qp, final_file=final_file, notes=notes)
    verb = "modified+approved" if final_file is not None else "approved"
    return _approval_status(qp, verb)


# --- $EDITOR integration ---


def edit_in_editor(content: str, *, suffix: str = ".tmp") -> str | None:
    """Open `content` in $EDITOR; return the edit if changed."""
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


# --- TUI ---


def cmd_supervise(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} supervise", add_help=True)
    parser.add_argument(
        "--once", action="store_true",
        help="list pending proposals once and exit (no TUI)",
    )
    args = parser.parse_args(argv)

    # Connect first, so a missing control plane is a clean error, not a curses
    # crash. Starts the orchestrator if needed.
    try:
        _client()
    except OrchestratorClientError as e:
        error(str(e))
        return 1
    except Die as e:
        # The backend has no orchestrator to start.
        if e.message:
            error(e.message)
        return e.code if isinstance(e.code, int) else 1

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
        error(
            f"supervise crashed: {type(e).__name__}: {e}",
            context={"error_type": type(e).__name__, "crash_log": str(log_path)},
        )
        error(f"full traceback written to {log_path}")
        return 1
    return 0


def _write_crash_log(exc: BaseException) -> Path:
    """Write `exc`'s traceback to a stable file under ~/.bot-bottle/."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    entry = f"=== supervise crash {stamp} ===\n{body}\n"
    try:
        log_dir = bot_bottle_root() / "logs"
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
            f"[{qp.label}]  "
            f"{qp.proposal.tool}  "
            f"{qp.proposal.id}\n"
        )
        sys.stdout.write(f"    {qp.proposal.justification}\n")
    return 0


def _try_init_green() -> int:  # pragma: no cover
    """A green color pair's attr, or 0."""
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        return curses.color_pair(1)
    except curses.error:
        return 0


def _main_loop(stdscr: "curses._CursesWindow") -> None:  # type: ignore  # pragma: no cover
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
            status_line = _detail_view(stdscr, qp, green_attr=green_attr)
        elif key == ord("a"):
            try:
                status_line = _approve_from_tui(stdscr, qp)
            except ApplyError as e:
                status_line = f"apply failed: {e}"
        elif key == ord("m"):
            if qp.proposal.tool in _REPORT_ONLY_TOOLS:
                status_line = f"modify unavailable for {qp.proposal.tool}"
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
                status_line = f"rejected {qp.proposal.tool} for [{qp.label}]"
            else:
                status_line = "reject aborted (empty reason)"


def _render(
    stdscr: "curses._CursesWindow",  # type: ignore
    pending: list[QueuedProposal],
    selected: int,
    status_line: str,
    *,
    green_attr: int = 0,  # noqa: F841 — unused, but required by interface
) -> None:  # pragma: no cover
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
                f"[{qp.label}]  {p.tool:<18}  {p.id[:8]}"
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
) -> str:  # pragma: no cover
    """Render the full proposal, scrollable. q returns."""
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
            return ""
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
                return _approve_from_tui(stdscr, qp)
            except ApplyError as exc:
                return f"apply failed: {exc}"
        elif key == ord("m"):
            if qp.proposal.tool in _REPORT_ONLY_TOOLS:
                return f"modify unavailable for {qp.proposal.tool}"
            edited = _modify(stdscr, qp)
            if edited is None:
                return "modify aborted (no change)"
            try:
                return _approve_from_tui(
                    stdscr, qp, final_file=edited,
                    notes="operator modified before approving",
                )
            except ApplyError as exc:
                return f"apply failed: {exc}"
        elif key == ord("r"):
            reason = _prompt(stdscr, "reject reason: ")
            if reason:
                reject(qp, reason=reason)
                return f"rejected {qp.proposal.tool} for [{qp.label}]"
            return "reject aborted (empty reason)"


def _modify(
    stdscr: "curses._CursesWindow",  # type: ignore
    qp: QueuedProposal,
) -> str | None:  # pragma: no cover
    """Suspend curses and edit the proposed file in $EDITOR."""
    suffix = _suffix_for_tool(qp.proposal.tool)
    curses.endwin()
    try:
        edited = edit_in_editor(qp.proposal.proposed_file, suffix=suffix)
    finally:
        stdscr.refresh()
    return edited


def _prompt(stdscr: "curses._CursesWindow", label: str) -> str:  # type: ignore  # pragma: no cover
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
