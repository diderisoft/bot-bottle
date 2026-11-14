"""The worktrees project launches from the console run in (PRD 0131).

A console launch of a project runs at the commit the console showed, in a
detached, locked worktree of the checkout under
`<root>/project-worktrees/<agent id>/`, so the checkout the user works in is
never switched. The host DB's `project_launches` keeps one row per launch,
what resume and cleanup read: the worktree goes when its bottle is torn
down, and comes back from the row when the bottle resumes. On start, the
host controller removes every worktree whose bottle isn't running; a launch
it interrupted is reported failed on the next console connect.
"""

from __future__ import annotations

import dataclasses
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from ...bottle_state import read_metadata
from ...lib import log
from ...lib.db_store import MACHINE_SPECIFIC, DbStore
from ...lib.migrations import TableMigrations
from ...manifest.util import ManifestError
from ...paths import bot_bottle_root, host_db_path
from .checkouts import git

WORKTREES_DIR = "project-worktrees"
# A launch's state: resolving or launching, waiting on the console's trust
# answer, or started (kept, the worktree removed at teardown, for a resume).
# `errored`: ended by a restart, to tell the console on its next connect.
STARTING, TRUST, RUNNING, ERRORED = "starting", "trust", "running", "errored"
INTERRUPTED = "the host restarted before this launch finished; try again"
GONE = "its project checkout is gone; start fresh instead"
AGENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", re.ASCII)
_GIT_SECONDS = 60.0


class WorktreeError(Exception):
    """git couldn't make the worktree: a line for a person."""


@dataclass(frozen=True)
class Launch:
    agent_id: str  # the console's id, and the worktree's directory name
    checkout: str  # the kept checkout's path, as reported
    repo: str
    branch: str
    commit: str
    worktree: str
    prefix: str  # the checkout's path in its repo, "" at the top
    state: str
    created_at: float
    bottle: str = ""  # once started
    since: str = ""  # when the pending trust prompt began
    pending: str = ""  # JSON: the prompt's items and records, and the request
    message: str = ""  # an `errored` launch's reason

    def cwd(self) -> Path:
        """Where the launch runs: the checkout's place in the worktree."""
        return Path(self.worktree) / self.prefix if self.prefix else Path(self.worktree)


_COLUMNS = tuple(f.name for f in dataclasses.fields(Launch))


class ProjectLaunchStore(DbStore):
    """`project_launches`: one row per project launch from the console."""

    # Paths on this host.
    TABLES = {"project_launches": MACHINE_SPECIFIC}

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, TableMigrations("project_launches", [
            """
            CREATE TABLE project_launches (
                agent_id TEXT PRIMARY KEY,
                checkout TEXT NOT NULL,
                repo TEXT NOT NULL,
                branch TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                worktree TEXT NOT NULL,
                prefix TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at REAL NOT NULL,
                bottle TEXT NOT NULL DEFAULT '',
                since TEXT NOT NULL DEFAULT '',
                pending TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT ''
            )
            """,
        ]))
        self.migrate()

    @staticmethod
    def _row(row: sqlite3.Row) -> Launch:
        values = {c: row["commit_sha" if c == "commit" else c] for c in _COLUMNS}
        return Launch(**values)

    def all(self) -> list[Launch]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM project_launches ORDER BY created_at").fetchall()
        return [self._row(r) for r in rows]

    def get(self, agent_id: str) -> Launch | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM project_launches WHERE agent_id = ?",
                               (agent_id,)).fetchone()
        return self._row(row) if row is not None else None

    def put(self, launch: Launch) -> None:
        with self._connection() as conn:
            conn.execute(f"INSERT OR REPLACE INTO project_launches VALUES "
                         f"({', '.join('?' * len(_COLUMNS))})",
                         tuple(getattr(launch, c) for c in _COLUMNS))
        self._chmod()

    def claim(self, agent_id: str, since: str, state: str) -> Launch | None:
        """Move a launch waiting on the trust prompt `since` to `state`, once:
        the launch, or None when it isn't waiting on that prompt (answered,
        timed out or asked again since)."""
        with self._connection() as conn:
            moved = conn.execute(
                "UPDATE project_launches SET state = ? "
                "WHERE agent_id = ? AND state = ? AND since = ?",
                (state, agent_id, TRUST, since)).rowcount
        return self.get(agent_id) if moved else None

    def delete(self, agent_id: str) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM project_launches WHERE agent_id = ?", (agent_id,))
        self._chmod()


def store() -> ProjectLaunchStore:
    return ProjectLaunchStore(host_db_path())


def root() -> Path:
    return bot_bottle_root() / WORKTREES_DIR


def worktree_for(agent_id: str) -> Path:
    return root() / agent_id


def has_commit(checkout: Path, commit: str) -> bool:
    out = git(checkout, "cat-file", "-e", f"{commit}^{{commit}}")
    return out.returncode == 0


def make(launch: Launch) -> None:
    """The launch's worktree: detached at its commit (a branch checked out in
    the checkout can't be checked out twice) and locked, so a `git worktree
    prune` the user runs leaves it. Raises WorktreeError."""
    worktree = Path(launch.worktree)
    if worktree.exists():
        remove(launch.checkout, worktree)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    reason = f"bot-bottle: {launch.agent_id}" + (f" ({launch.branch})" if launch.branch else "")
    try:
        out = git(Path(launch.checkout), "worktree", "add", "--detach", "--lock", "--reason",
                  reason, str(worktree), launch.commit, timeout=_GIT_SECONDS)
    except (OSError, subprocess.SubprocessError) as e:
        raise WorktreeError(f"couldn't run git in {launch.checkout}: {e}") from e
    if out.returncode != 0:
        why = (out.stderr.strip().splitlines() or ["git worktree add failed"])[-1]
        raise WorktreeError(f"couldn't make a worktree of {launch.checkout} at "
                            f"{launch.commit[:12]}: {why}")


def remove(checkout: str | None, worktree: Path) -> None:
    """Remove the worktree and its entry in its repo, then the directory.
    git runs from the worktree while it's there, so a checkout moved or gone
    since leaves no locked entry behind (`git worktree prune` keeps locked
    ones); `checkout` is pruned too when it's still there. Never raises."""
    if (worktree / ".git").is_file():
        at: Path | None = worktree
    else:
        at = Path(checkout) if checkout and Path(checkout).is_dir() else None
    try:
        if at is not None:
            git(at, "worktree", "unlock", str(worktree))
            git(at, "worktree", "remove", "--force", str(worktree), timeout=_GIT_SECONDS)
        if checkout and Path(checkout).is_dir():
            git(Path(checkout), "worktree", "prune")
    except (OSError, subprocess.SubprocessError) as e:
        log.warn(f"projects: couldn't remove the worktree {worktree}: {e}")
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)


def ended(launch: Launch, launches: ProjectLaunchStore) -> None:
    """Its bottle was torn down: remove the worktree. The row stays while the
    bottle's saved state does, for a resume."""
    remove(launch.checkout, Path(launch.worktree))
    if not launch.bottle or read_metadata(launch.bottle) is None:
        launches.delete(launch.agent_id)


def sweep(running: Collection[str], launches: ProjectLaunchStore | None = None) -> None:
    """On host controller start: remove each worktree whose bottle isn't one
    of `running`. A launch that was still starting, or waiting on trust, is
    kept as `errored`, for the console to hear on its next connect."""
    try:
        launches = launches or store()
        live: set[str] = set()
        for launch in launches.all():
            if launch.state == RUNNING and launch.bottle in running:
                live.add(launch.agent_id)
            elif launch.state in (STARTING, TRUST):
                remove(launch.checkout, Path(launch.worktree))
                launches.put(dataclasses.replace(launch, state=ERRORED, message=INTERRUPTED,
                                                 since="", pending=""))
            elif launch.state == RUNNING:
                ended(launch, launches)
        for worktree in root().iterdir() if root().is_dir() else ():
            if worktree.name not in live:  # with no row: its entry goes too
                remove(None, worktree)
    except (OSError, sqlite3.Error) as e:
        log.warn(f"projects: couldn't sweep the project worktrees: {e}")


def restore(cwd: str, launches: ProjectLaunchStore | None = None) -> str | None:
    """For a launch from a project launch's worktree (a resume): the
    checkout's path, the worktree made again first if it's gone. None for any
    other directory. ManifestError when it can't be."""
    try:
        agent_id = Path(cwd).relative_to(root()).parts[0]
    except (ValueError, IndexError):
        return None
    launch = (launches or store()).get(agent_id)
    if launch is None or launch.state not in (RUNNING, STARTING):
        raise ManifestError(GONE)
    if not Path(launch.worktree).is_dir():
        checkout = Path(launch.checkout)
        try:
            if not checkout.is_dir() or not has_commit(checkout, launch.commit):
                raise ManifestError(GONE)
            make(launch)
        except (WorktreeError, OSError, subprocess.SubprocessError) as e:
            log.warn(f"projects: {e}")
            raise ManifestError(GONE) from e
    return launch.checkout


def is_worktree(cwd: str) -> bool:
    """Whether `cwd` is in a project launch's worktree."""
    return Path(cwd).is_relative_to(root())


__all__ = ["AGENT_ID", "ERRORED", "GONE", "INTERRUPTED", "Launch", "ProjectLaunchStore",
           "RUNNING", "STARTING", "TRUST", "WORKTREES_DIR", "WorktreeError", "ended", "has_commit",
           "is_worktree", "make", "remove", "restore", "root", "store", "sweep", "worktree_for"]
