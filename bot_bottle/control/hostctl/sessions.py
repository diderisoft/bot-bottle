"""Agent sessions in tmux, owned by the host controller.

Sessions run on bot-bottle's own tmux server, launched with a command that
fetches the real argv and env from the daemon, so secrets never reach tmux. A
`pane-died` hook reports the exit; the finisher runs and the session is killed.
Records persist, so a restarted daemon re-adopts live sessions and finishes
dead ones.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ...lib import log
from ...paths import bot_bottle_root
from .build import PACKAGE_ROOT
from .runner import state_dir

MIN_TMUX_VERSION = (3, 1)
_SESSION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class SessionError(RuntimeError):
    """A tmux session couldn't be started or driven."""


@dataclass(frozen=True)
class ExitStatus:
    status: int
    signal: str = ""


@dataclass(frozen=True)
class SessionRecord:
    """A session's persisted record. `kind` picks the finisher; `meta` is its
    input."""

    id: str
    kind: str
    meta: dict[str, object] = field(default_factory=dict)
    writable_allowed: bool = False


def socket_name() -> str:
    """bot-bottle's tmux server name, per root, so roots never share sessions."""
    root = bot_bottle_root().resolve()
    if root == (Path.home() / ".bot-bottle").resolve():
        return "bot-bottle"
    return "bot-bottle-" + hashlib.sha256(str(root).encode()).hexdigest()[:8]


def config_path() -> Path:
    return state_dir() / "tmux.conf"


def control_socket_path() -> Path:
    """The daemon socket the launcher and exit hook use."""
    return state_dir() / "sessions.sock"


def tmux(*args: str) -> list[str]:
    return ["tmux", "-L", socket_name(), "-f", str(config_path()), *args]


def tmux_version() -> tuple[int, int] | None:
    """Installed tmux (major, minor), or None."""
    try:
        out = subprocess.run(["tmux", "-V"], capture_output=True, text=True, check=False).stdout
    except OSError:
        return None
    match = re.search(r"(\d+)\.(\d+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _python_command(module: str, *args: str) -> str:
    """Shell command running `python -m module args` from this package, never
    from the working directory (`-P`, #706)."""
    argv = ["env", f"PYTHONPATH={PACKAGE_ROOT.parent}", sys.executable, "-P", "-m", module,
            *args]
    return shlex.join(argv)


# Status bar: copper "bot-bottle", the session label and agent on the left, the
# detach key on the right. `@bb_agent` is empty when the label is the agent;
# `@bb_warning` is the launch's warnings, shortened (see `status_warning`).
# Fits the client: the full block notice and the detach hint need 150 columns,
# and under 100 the request summary and the warning are cut (PRD 0099).
_WIDE = "#{e|>=:#{client_width},150}"
_NARROW = "#{e|<:#{client_width},100}"
# What the bottle blocked (`blocks.py`): the latest block, red, for a few
# seconds, beside the session's counts by reason in one dim colour.
_BLOCKS = (
    "#{?@bb_blocks,"
    "#{?@bb_block_now,"
    f"#[bg=colour88#,fg=colour255#,bold] ⊘ #{{?{_WIDE},#{{@bb_block_now}},#{{@bb_block_host}}}} "
    "#[default] ,#[fg=colour131]⊘ }"
    "#[fg=colour131]#{@bb_blocks} #[default],}"
)
# A pending request turns its segment copper, with the review key; otherwise a
# launch warning in amber, then the detach key, when there's room.
_REQUEST = (
    "#{?@bb_request,"
    f"#[bg=colour173#,fg=colour232#,bold] #{{?{_NARROW},#{{=30:@bb_request}},#{{@bb_request}}}}"
    " · Ctrl-\\\\ review ,"
    f"#{{?@bb_warning,#[fg=colour178]⚠ #{{?{_NARROW},#{{=/30/…:@bb_warning}},"
    "#{=/70/…:@bb_warning}}  ,}"
    f"#{{?#{{||:{_WIDE},#{{?@bb_blocks,0,1}}}},#[fg=colour244]Ctrl-] detach ,}}}}"
)
_STATUS_BAR = (
    "set -g status on",
    "set -g status-position bottom",
    'set -g status-style "bg=colour235,fg=colour250"',
    "set -g status-left-length 100",
    "set -g status-right-length 160",
    # Requests and blocks arrive as option changes; redraw every second.
    "set -g status-interval 1",
    'set -g status-left "#[fg=colour173,bold] ▣ bot-bottle #[default]· #{session_name}'
    '#{?@bb_agent, · #{@bb_agent} agent,} "',
    f'set -g status-right "{_BLOCKS}{_REQUEST}"',
    'set -g window-status-format ""',
    'set -g window-status-current-format ""',
)


def status_warning(warnings: Sequence[str]) -> str:
    """The status bar's form of a launch's warnings: the first one up to its
    first clause, a count of the rest, and `#` doubled so tmux shows it
    rather than reading a style or format from it."""
    if not warnings:
        return ""
    first = re.split(r";|\. ", warnings[0].strip(), maxsplit=1)[0].rstrip(".")
    more = f" (+{len(warnings) - 1} more)" if len(warnings) > 1 else ""
    return (first + more).replace("#", "##")


def write_config() -> Path:
    """bot-bottle's tmux config: no prefix, Ctrl-] detaches, the most recent
    client sets the size, dead panes stay until reported, and a status bar
    outside the agent's pane names the session."""
    # A wrapper script, so the config line holds one quoted path.
    notify = state_dir() / "notify"
    notify.parent.mkdir(parents=True, exist_ok=True)
    notify.write_text("#!/bin/sh\nexec " + _python_command(
        "bot_bottle.control.hostctl.notify", str(control_socket_path())) + ' "$@"\n',
        encoding="utf-8")
    notify.chmod(0o700)
    hook = shlex.quote(str(notify)) + " #{q:session_name} #{pane_dead_status} #{pane_dead_signal}"
    approve = state_dir() / "approve"
    approve.write_text("#!/bin/sh\nexec " + _python_command("bot_bottle.cli", "approve")
                       + ' "$@"\n', encoding="utf-8")
    approve.chmod(0o700)
    lines = [
        *_STATUS_BAR,
        "set -g prefix None",
        "set -g prefix2 None",
        "unbind C-b",
        "bind -n C-] detach-client",
        # Ctrl-\ opens a pending request's review popup; otherwise it goes to
        # the agent.
        "bind -n 'C-\\' if-shell -F '#{@bb_request}' "
        + '"display-popup -E -w 90% -h 70% -T \' bot-bottle request \' '
        + "'" + str(approve) + " #{session_name}'\" 'send-keys C-\\'",
        "set -g window-size latest",
        "set -g remain-on-exit on",
        "set -g default-terminal tmux-256color",
        "set -g escape-time 10",
        # Viewers (local, WebSocket, the phone) are tmux clients, so they get
        # tmux's redraw, never the agent's raw bytes. Pin what keeps an
        # agent's escapes from reaching them: no clipboard writes (OSC 52)
        # and no passthrough (DCS), whatever tmux's defaults become.
        "set -g set-clipboard off",
        "set -g allow-passthrough off",
        'set-hook -g pane-died "run-shell \\"' + hook.replace('"', '\\"') + '\\""',
    ]
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class SessionManager:
    def __init__(self, finishers: dict[str, Callable[[SessionRecord, ExitStatus], None]]) -> None:
        self._finishers = finishers
        self._specs: dict[str, tuple[list[str], dict[str, str]]] = {}
        self._exits: dict[str, ExitStatus] = {}
        self._finishing: set[str] = set()
        self._cond = threading.Condition()

    def register(self, kind: str,
                 finisher: Callable[[SessionRecord, ExitStatus], None]) -> None:
        """Run `finisher` when a session of `kind` ends."""
        self._finishers[kind] = finisher

    # --- records ---

    def records(self) -> list[SessionRecord]:
        """Every session record on disk."""
        directory = state_dir() / "sessions"
        found = (self.record(p.stem) for p in sorted(directory.glob("*.json"))) \
            if directory.is_dir() else iter(())
        return [r for r in found if r is not None]

    def _record_path(self, session_id: str) -> Path:
        return state_dir() / "sessions" / f"{session_id}.json"

    def record(self, session_id: str) -> SessionRecord | None:
        try:
            raw = json.loads(self._record_path(session_id).read_text(encoding="utf-8"))
            return SessionRecord(**raw)
        except (OSError, ValueError, TypeError):
            return None

    def _write_record(self, record: SessionRecord) -> None:
        path = self._record_path(record.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(record)), encoding="utf-8")
        tmp.replace(path)

    # --- lifecycle ---

    def start(self, record: SessionRecord, argv: list[str], env: dict[str, str],
              warnings: Sequence[str] = ()) -> None:
        """Start `argv` with `env` as session `record.id`."""
        if not _SESSION_ID.match(record.id):
            raise SessionError(f"invalid session id {record.id!r}")
        if self.exists(record.id):
            raise SessionError(f"session {record.id} already exists")
        write_config()
        self._write_record(record)
        with self._cond:
            self._specs[record.id] = (list(argv), dict(env))
            self._exits.pop(record.id, None)
        launcher = _python_command(
            "bot_bottle.control.hostctl.launch", str(control_socket_path()), record.id)
        # The agent name for the status bar rides in the same tmux command.
        agent = str(record.meta.get("agent", ""))
        shown = [";", "set-option", "-t", f"={record.id}:", "@bb_agent", agent] \
            if agent and agent != record.id else []
        warning = status_warning(warnings)
        if warning:
            shown += [";", "set-option", "-t", f"={record.id}:", "@bb_warning", warning]
        proc = subprocess.run(
            tmux("new-session", "-d", "-s", record.id, "-x", "120", "-y", "40", launcher, *shown),
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            with self._cond:
                self._specs.pop(record.id, None)
            self._record_path(record.id).unlink(missing_ok=True)
            raise SessionError(f"tmux new-session failed: {proc.stderr.strip()}")

    def spec(self, session_id: str) -> tuple[list[str], dict[str, str]] | None:
        """The command and env for a starting session, handed out once."""
        with self._cond:
            return self._specs.pop(session_id, None)

    def died(self, session_id: str, exit_status: ExitStatus) -> None:
        """`session_id`'s agent exited: run its finisher and kill the session.
        The launcher and the hook both report; the first wins."""
        with self._cond:
            if session_id in self._finishing or session_id in self._exits:
                return
            self._finishing.add(session_id)
        record = self.record(session_id)
        if record is None:
            with self._cond:
                self._finishing.discard(session_id)
            log.warn(f"tmux session {session_id} died but has no record; leaving it")
            return
        finisher = self._finishers.get(record.kind)
        try:
            if finisher is not None:
                finisher(record, exit_status)
        except Exception as e:  # pylint: disable=broad-exception-caught
            log.warn(f"finishing session {session_id} failed: {e}")
        finally:
            subprocess.run(tmux("kill-session", "-t", f"={session_id}"),
                           capture_output=True, check=False)
            self._record_path(session_id).unlink(missing_ok=True)
            with self._cond:
                self._exits[session_id] = exit_status
                self._finishing.discard(session_id)
                self._cond.notify_all()

    def wait_exit(self, session_id: str, timeout: float | None = None) -> ExitStatus | None:
        with self._cond:
            self._cond.wait_for(lambda: session_id in self._exits, timeout)
            return self._exits.get(session_id)

    def exit_status(self, session_id: str) -> ExitStatus | None:
        with self._cond:
            return self._exits.get(session_id)

    def kill(self, session_id: str) -> None:
        """Kill the tmux session; the agent gets SIGHUP and the exit is
        reported."""
        subprocess.run(tmux("kill-session", "-t", f"={session_id}"),
                       capture_output=True, check=False)

    def exists(self, session_id: str) -> bool:
        return subprocess.run(tmux("has-session", "-t", f"={session_id}"),
                              capture_output=True, check=False).returncode == 0

    def adopt(self) -> None:
        """After a daemon start: finish sessions that died meanwhile, drop
        records of gone sessions, report unknown ones."""
        proc = subprocess.run(
            tmux("list-panes", "-a", "-F",
                 "#{session_name} #{pane_dead} #{pane_dead_status} #{pane_dead_signal}"),
            capture_output=True, text=True, check=False,
        )
        live: dict[str, tuple[bool, int, str]] = {}
        for line in proc.stdout.splitlines() if proc.returncode == 0 else []:
            name, dead, status, sig = (line.split(" ") + ["", "", ""])[:4]
            live[name] = (dead == "1", int(status) if status.isdigit() else 0, sig)
        records_dir = state_dir() / "sessions"
        known = {p.stem for p in records_dir.glob("*.json")} if records_dir.is_dir() else set()
        for name, (dead, status, sig) in live.items():
            if name not in known:
                log.warn(f"tmux session {name} isn't one this host controller started; leaving it")
            elif dead:
                self.died(name, ExitStatus(status, sig))
        for name in known - set(live):
            self._record_path(name).unlink(missing_ok=True)

    # --- viewers ---

    def attach_argv(self, session_id: str, *, read_only: bool) -> list[str]:
        return tmux("attach-session", *(["-r"] if read_only else []), "-t", f"={session_id}")

    def history(self, session_id: str, lines: int) -> tuple[str, int, int]:
        """The last `lines` of scrolled-off pane history with colors, plus the
        history's size and limit from the same tmux command. ("", 0, 0) without
        the session."""
        target = f"={session_id}:"
        proc = subprocess.run(
            tmux("display-message", "-p", "-t", target, "#{history_size} #{history_limit}", ";",
                 "capture-pane", "-p", "-e", "-S", f"-{lines}", "-E", "-1", "-t", target),
            capture_output=True, text=True, errors="replace", check=False)
        head, _, text = proc.stdout.partition("\n")
        try:
            size, limit = (int(n) for n in head.split())
        except ValueError:
            return "", 0, 0
        return (text if size else ""), size, limit

    def set_read_only(self, client_tty: str, read_only: bool) -> None:
        """Make the viewer on `client_tty` read-only or writable
        (`switch-client -r` toggles, so only on a change). It names the
        client's own session: without `-t`, tmux also switches the client to
        the most recently used session, so typing would reach another agent."""
        proc = subprocess.run(
            tmux("list-clients", "-F", "#{client_tty} #{client_readonly} #{client_session}"),
            capture_output=True, text=True, check=False)
        for line in proc.stdout.splitlines():
            tty, flag, session = (line.split(" ", 2) + ["", ""])[:3]
            if tty == client_tty and session and (flag == "1") != read_only:
                subprocess.run(tmux("switch-client", "-c", client_tty, "-t", f"={session}", "-r"),
                               capture_output=True, check=False)
                return


__all__ = [
    "ExitStatus", "MIN_TMUX_VERSION", "SessionError", "SessionManager", "SessionRecord",
    "config_path", "control_socket_path", "socket_name", "tmux", "tmux_version", "write_config",
]
