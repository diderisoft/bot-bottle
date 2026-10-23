"""Agent sessions in tmux, owned by the host controller.

Each session runs on bot-bottle's own tmux server (never the user's), started
detached with a launcher that fetches the real command and environment from
the daemon, so secrets never reach tmux's argv or environment. A `pane-died`
hook reports the agent's exit; the session's finisher then runs and the tmux
session is killed, detaching every viewer. Session records persist under the
state dir, so a restarted daemon re-adopts live sessions and finishes ones that
died while it was down.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import sys
import threading
from collections.abc import Callable
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
    """What the daemon persists about a session. `kind` picks the finisher
    that runs when the agent exits; `meta` is that finisher's input."""

    id: str
    kind: str
    meta: dict[str, object] = field(default_factory=dict)
    writable_allowed: bool = False


def socket_name() -> str:
    """bot-bottle's tmux server: `bot-bottle` for the default root, a
    per-root name otherwise, so separate roots never share sessions."""
    root = bot_bottle_root().resolve()
    if root == (Path.home() / ".bot-bottle").resolve():
        return "bot-bottle"
    return "bot-bottle-" + hashlib.sha256(str(root).encode()).hexdigest()[:8]


def config_path() -> Path:
    return state_dir() / "tmux.conf"


def control_socket_path() -> Path:
    """The daemon's local socket the launcher and the exit hook talk to."""
    return state_dir() / "sessions.sock"


def tmux(*args: str) -> list[str]:
    return ["tmux", "-L", socket_name(), "-f", str(config_path()), *args]


def tmux_version() -> tuple[int, int] | None:
    """The installed tmux's (major, minor), or None if it isn't installed."""
    try:
        out = subprocess.run(["tmux", "-V"], capture_output=True, text=True, check=False).stdout
    except OSError:
        return None
    match = re.search(r"(\d+)\.(\d+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _python_command(module: str, *args: str) -> str:
    """A shell command running `python -m module args` from this package."""
    argv = ["env", f"PYTHONPATH={PACKAGE_ROOT.parent}", sys.executable, "-m", module, *args]
    return shlex.join(argv)


# The status bar: copper "bot-bottle", the session (its label) and agent on the
# left, the detach key on the right, no window list. `@bb_agent` is set per
# session at start, and left empty when the label already is the agent.
_STATUS_BAR = (
    "set -g status on",
    "set -g status-position bottom",
    'set -g status-style "bg=colour235,fg=colour250"',
    "set -g status-left-length 100",
    "set -g status-right-length 40",
    'set -g status-left "#[fg=colour173,bold] ▣ bot-bottle #[default]· #{session_name}'
    '#{?@bb_agent, · #{@bb_agent} agent,} "',
    'set -g status-right "#[fg=colour244]Ctrl-] detach "',
    'set -g window-status-format ""',
    'set -g window-status-current-format ""',
)


def write_config() -> Path:
    """bot-bottle's tmux config: no prefix, Ctrl-] detaches, the most
    recently active client sets the size, and dead panes stay until the
    `pane-died` hook has reported them. A one-line status bar at the bottom
    says this is a bot-bottle session (which one, and its agent) and how to
    detach. tmux draws it outside the agent's pane, so the agent can't
    draw over it or fake it."""
    # The hook runs a wrapper script so the config line holds one quoted path.
    notify = state_dir() / "notify"
    notify.parent.mkdir(parents=True, exist_ok=True)
    notify.write_text("#!/bin/sh\nexec " + _python_command(
        "bot_bottle.control.hostctl.notify", str(control_socket_path())) + ' "$@"\n',
        encoding="utf-8")
    notify.chmod(0o700)
    hook = shlex.quote(str(notify)) + " #{q:session_name} #{pane_dead_status} #{pane_dead_signal}"
    lines = [
        *_STATUS_BAR,
        "set -g prefix None",
        "set -g prefix2 None",
        "unbind C-b",
        "bind -n C-] detach-client",
        "set -g window-size latest",
        "set -g remain-on-exit on",
        "set -g default-terminal tmux-256color",
        "set -g escape-time 10",
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

    # -- records -------------------------------------------------------------

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

    # -- lifecycle -----------------------------------------------------------

    def start(self, record: SessionRecord, argv: list[str], env: dict[str, str]) -> None:
        """Start `argv` with `env` (added to tmux's environment) as session `record.id`."""
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
        proc = subprocess.run(
            tmux("new-session", "-d", "-s", record.id, "-x", "120", "-y", "40", launcher),
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            with self._cond:
                self._specs.pop(record.id, None)
            self._record_path(record.id).unlink(missing_ok=True)
            raise SessionError(f"tmux new-session failed: {proc.stderr.strip()}")
        agent = str(record.meta.get("agent", ""))
        if agent and agent != record.id:
            # Only for the status bar; a failure here doesn't stop the agent.
            subprocess.run(tmux("set-option", "-t", f"={record.id}:", "@bb_agent", agent),
                           capture_output=True, check=False)

    def spec(self, session_id: str) -> tuple[list[str], dict[str, str]] | None:
        """The command and environment for a starting session, handed out once."""
        with self._cond:
            return self._specs.pop(session_id, None)

    def died(self, session_id: str, exit_status: ExitStatus) -> None:
        """The agent in `session_id` exited: run its finisher, kill the session.
        Reported by both the launcher and the `pane-died` hook; the first wins."""
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
        """Kill the tmux session; its agent gets SIGHUP and the exit is
        reported as usual."""
        subprocess.run(tmux("kill-session", "-t", f"={session_id}"),
                       capture_output=True, check=False)

    def exists(self, session_id: str) -> bool:
        return subprocess.run(tmux("has-session", "-t", f"={session_id}"),
                              capture_output=True, check=False).returncode == 0

    def adopt(self) -> None:
        """After a daemon (re)start: finish sessions whose agent died while
        it was down, drop records whose session is gone, report unknown sessions."""
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

    # -- viewers -------------------------------------------------------------

    def attach_argv(self, session_id: str, *, read_only: bool) -> list[str]:
        return tmux("attach-session", *(["-r"] if read_only else []), "-t", f"={session_id}")

    def set_read_only(self, client_tty: str, read_only: bool) -> None:
        """Make the viewer on `client_tty` read-only or writable. `switch-client
        -r` toggles, so it runs only when the state differs."""
        proc = subprocess.run(tmux("list-clients", "-F", "#{client_tty} #{client_readonly}"),
                              capture_output=True, text=True, check=False)
        for line in proc.stdout.splitlines():
            tty, _, flag = line.partition(" ")
            if tty == client_tty and (flag == "1") != read_only:
                subprocess.run(tmux("switch-client", "-c", client_tty, "-r"),
                               capture_output=True, check=False)
                return


__all__ = [
    "ExitStatus", "MIN_TMUX_VERSION", "SessionError", "SessionManager", "SessionRecord",
    "config_path", "control_socket_path", "socket_name", "tmux", "tmux_version", "write_config",
]
