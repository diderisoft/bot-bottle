"""The project checkouts this host keeps (PRD 0131), in the host DB
(`project_checkouts`), for the console's list of projects.

A checkout is a directory holding a project's `.bot-bottle/`, inside a git
work tree; its project is its remote's URL, normalized
(`normalize_repo_url`), so the SSH and HTTPS forms of one repo agree. The
host learns one from `bot-bottle project add` or a project launch there,
never by scanning the disk, and drops one that's gone (`refresh`). The
console hears them all (`project_link`).
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ...lib import log
from ...lib.db_store import MACHINE_SPECIFIC, DbStore
from ...lib.migrations import TableMigrations
from ...manifest.versions import git_env
from ...paths import host_db_path, project_manifest_dir

# What the console takes of a report.
MAX_CHECKOUTS = 500
MAX_PATH = 1024
MAX_BRANCH = 255
# A normalized repo, as the console checks it.
REPO_RE = re.compile(
    r"^[a-z0-9\[][a-z0-9.\-\[\]:]{0,252}(/(?!\.\.?(/|$))[a-z0-9._~\-+%]{1,200}){2,16}$")
_SCHEMES = frozenset({"ssh", "git", "http", "https", "git+ssh", "ssh+git"})
_SCP = re.compile(r"(?:[^@/]+@)?(\[[^\]/]+\]|[^:/\[\]@]+):(.*)", re.DOTALL)
_GIT_SECONDS = 10.0


def normalize_repo_url(url: str) -> str | None:
    """`host/owner/repo` (more segments for GitLab subgroups), lowercased,
    without userinfo, port or `.git`; None for a local path or anything else
    that names no forge repo."""
    url = url.strip()
    if "://" in url:
        scheme, rest = url.split("://", 1)
        if scheme.lower() not in _SCHEMES:
            return None
        authority, _, path = rest.partition("/")
        host = authority.rpartition("@")[2]
        if host.startswith("["):
            end = host.find("]")
            host = host[:end + 1] if end > 0 else ""
        else:
            host = host.partition(":")[0]
    else:
        scp = _SCP.fullmatch(url)
        if scp is None or scp.group(2).startswith("/"):
            return None
        host, path = scp.group(1), scp.group(2)
    segments = [s for s in path.split("/") if s]
    if segments:
        segments[-1] = segments[-1].removesuffix(".git")
    if not host or len(segments) < 2 or not segments[-1]:
        return None
    repo = (host + "/" + "/".join(segments)).lower()
    return repo if REPO_RE.fullmatch(repo) else None


def git(cwd: Path, *args: str, timeout: float = _GIT_SECONDS) -> subprocess.CompletedProcess[str]:
    """git in `cwd`, as the host runs it for itself (no prompts). Raises
    OSError or TimeoutExpired when git can't run."""
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          env=git_env(), check=False, timeout=timeout)


def remote_name(cwd: Path) -> str:
    """The checkout's remote: `origin`, else the only one, else ""."""
    names = git(cwd, "remote").stdout.split()
    return "origin" if "origin" in names else (names[0] if len(names) == 1 else "")


@dataclass(frozen=True)
class Found:
    """What a checkout is now."""
    repo: str
    branch: str  # "" when detached


@dataclass(frozen=True)
class Refused:
    """Why a path isn't a checkout this host can keep."""
    reason: str


def inspect(path: Path) -> Found | Refused:
    """The checkout at `path`: its repo and branch, or why it isn't one.
    Raises OSError or TimeoutExpired when git can't run."""
    if not path.is_dir():
        return Refused(f"{path} isn't a directory")
    inside = git(path, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return Refused(f"{path} isn't in a git work tree")
    remote = remote_name(path)
    url = git(path, "remote", "get-url", remote).stdout.strip() if remote else ""
    repo = normalize_repo_url(url) if url else None
    if repo is None:
        why = (f"its remote's URL ({url}) names no forge repo" if url else
               "it has no `origin` remote (or only remote)")
        return Refused(f"{path} has no repo: {why}")
    branch = git(path, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    return Found(repo, branch)


def iso(ts: float | None) -> str | None:
    return (datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if ts is not None else None)


@dataclass(frozen=True)
class Checkout:
    path: str  # absolute and resolved: the key the console sends back
    repo: str
    branch: str
    added_at: float  # time.time()
    last_used_at: float | None  # the last launch there, if any

    def to_json(self) -> dict[str, object]:
        """As the console's `projects` report gives it."""
        return {"path": self.path, "repo": self.repo, "branch": self.branch,
                "added": iso(self.added_at), "last_used": iso(self.last_used_at)}

    def reportable(self) -> bool:
        return (Path(self.path).is_absolute() and len(self.path) <= MAX_PATH
                and len(self.branch) <= MAX_BRANCH and bool(REPO_RE.fullmatch(self.repo)))


class CheckoutStore(DbStore):
    """`project_checkouts`: one row per checkout, keyed by its path."""

    # Paths on this host.
    TABLES = {"project_checkouts": MACHINE_SPECIFIC}

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, TableMigrations("project_checkouts", [
            """
            CREATE TABLE project_checkouts (
                path TEXT PRIMARY KEY,
                repo TEXT NOT NULL,
                branch TEXT NOT NULL DEFAULT '',
                added_at REAL NOT NULL,
                last_used_at REAL
            )
            """,
        ]))
        self.migrate()

    @staticmethod
    def _row(row: sqlite3.Row) -> Checkout:
        return Checkout(path=row["path"], repo=row["repo"], branch=row["branch"],
                        added_at=float(row["added_at"]), last_used_at=row["last_used_at"])

    def all(self) -> list[Checkout]:
        """Every checkout, most recently used (else added) first."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM project_checkouts "
                "ORDER BY COALESCE(last_used_at, added_at) DESC, path").fetchall()
        return [self._row(r) for r in rows]

    def get(self, path: str) -> Checkout | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM project_checkouts WHERE path = ?",
                               (path,)).fetchone()
        return self._row(row) if row is not None else None

    def keep(self, path: str, found: Found, *, used: bool = False) -> Checkout:
        """Add the checkout, or refresh its repo and branch; `used`: a launch
        there, now."""
        now = time.time()
        with self._write_connection() as conn:
            conn.execute(
                "INSERT INTO project_checkouts VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET repo = excluded.repo, "
                "branch = excluded.branch, "
                "last_used_at = COALESCE(excluded.last_used_at, last_used_at)",
                (path, found.repo, found.branch, now, now if used else None))
            row = conn.execute("SELECT * FROM project_checkouts WHERE path = ?",
                               (path,)).fetchone()
        self._chmod()
        return self._row(row)

    def update(self, path: str, found: Found) -> None:
        """A kept checkout's repo and branch now (never re-adding a removed one)."""
        with self._connection() as conn:
            conn.execute("UPDATE project_checkouts SET repo = ?, branch = ? WHERE path = ?",
                         (found.repo, found.branch, path))
        self._chmod()

    def remove(self, path: str) -> bool:
        """Forget the checkout; whether it was kept."""
        with self._connection() as conn:
            removed = conn.execute("DELETE FROM project_checkouts WHERE path = ?",
                                   (path,)).rowcount > 0
        self._chmod()
        return removed


def store() -> CheckoutStore:
    return CheckoutStore(host_db_path())


def resolved(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def refresh(checkouts: CheckoutStore) -> bool:
    """Re-read each checkout: one gone from disk, out of a work tree or with
    no repo now is dropped, the rest get their repo and branch now. Whether
    anything changed. One git can't read is left as it was."""
    changed = False
    for kept in checkouts.all():
        try:
            now = inspect(Path(kept.path))
        except (OSError, subprocess.SubprocessError) as e:
            log.debug(f"projects: can't read {kept.path}: {e}")
            continue
        if isinstance(now, Refused):
            log.info(f"projects: dropped {kept.path}: {now.reason}")
            changed = checkouts.remove(kept.path) or changed
        elif (now.repo, now.branch) != (kept.repo, kept.branch):
            checkouts.update(kept.path, now)
            changed = True
    return changed


def report(checkouts: CheckoutStore) -> dict[str, object]:
    """The `projects` message: every checkout, within what the console keeps."""
    rows = [c.to_json() for c in checkouts.all() if c.reportable()]
    return {"type": "projects", "checkouts": rows[:MAX_CHECKOUTS]}


def learn_launch(cwd: str, checkouts: CheckoutStore | None = None) -> None:
    """A launch from `cwd`: when it's a project with a repo, keep it as used
    now. Never fails the launch."""
    try:
        if project_manifest_dir(cwd) is None:
            return
        path = resolved(cwd)
        found = inspect(Path(path))
        if isinstance(found, Found):
            (checkouts or store()).keep(path, found, used=True)
    except Exception as e:  # pylint: disable=broad-exception-caught  # best effort
        log.debug(f"projects: couldn't keep the launch's checkout {cwd}: {e}")


# --- the host controller's routes (`PROJECTS_PATH`, `PROJECTS_REMOVE_PATH`) ---


def _path(body: dict[str, object]) -> str:
    path = body.get("path")
    if not isinstance(path, str) or not path or not Path(path).is_absolute():
        raise ValueError("path must be an absolute path")
    return resolved(path)


def listed(_body: dict[str, object]) -> dict[str, object]:
    """Every kept checkout, as the console hears them."""
    return {"checkouts": [c.to_json() for c in store().all()]}


def add(body: dict[str, object]) -> dict[str, object]:
    """Keep the checkout at `path` (adding one kept refreshes it): `checkout`,
    and `note` when it has no bottles yet. ValueError says why it can't be."""
    path = _path(body)
    try:
        found = inspect(Path(path))
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError(f"can't run git in {path}: {e}") from e
    if isinstance(found, Refused):
        raise ValueError(found.reason)
    checkout = store().keep(path, found)
    note = "" if (Path(path) / ".bot-bottle").is_dir() else (
        f"no bottles there yet: they go in {Path(path) / '.bot-bottle' / 'bottles'}")
    return {"checkout": checkout.to_json(), "note": note}


def remove(body: dict[str, object]) -> dict[str, object]:
    """Forget the checkout at `path`: `{"removed": bool}`."""
    return {"removed": store().remove(_path(body))}


__all__ = ["Checkout", "CheckoutStore", "Found", "MAX_BRANCH", "MAX_CHECKOUTS", "MAX_PATH",
           "REPO_RE", "Refused", "add", "git", "inspect", "iso", "learn_launch", "listed",
           "normalize_repo_url", "refresh", "remote_name", "remove", "report", "resolved",
           "store"]
