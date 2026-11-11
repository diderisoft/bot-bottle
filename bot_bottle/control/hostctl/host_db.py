"""The host DB, `<root>/db/bot-bottle.db`: the host's source-of-truth state
(ADR 0020). The host controller is its only writer. When it starts it checks
the file (`PRAGMA quick_check`), refusing to start on a failed check,
migrates it without asking, dropping any table no host store owns, and takes
the day's backup. The orchestrator's tables
are in its own `registry.db`, and indexes derived from logs in `index.db`
(`index_db.py`), never here.

Backups are `VACUUM INTO` copies in `<root>/db/backups/` (0700, files 0600),
one a day, the newest `KEEP_BACKUPS` kept. `bot-bottle host restore-db` puts
one back while the host controller is stopped.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ...backend.firecracker.workspace_disk import WorkspaceDiskStore
from ...backend.isolation_probe import IsolationPasses
from ...bottle_state import BottledAgentStore
from ...forge_connections import ForgeStore
from ...infra_state import InfraStateStore
from ...lib import log
from ...lib.config_store import ConfigStore
from ...lib.db_store import DbStore, connect, drop_unowned, quick_check
from ...manifest.trust import TrustStore
from ...paths import host_db_path
from ...run_manifest import RunManifestStore
from .audit_forward import ForwardCursors
from .agent_prefs import AgentPrefsStore
from .audit_push import AuditPushStore
from .audit_trust import PasskeyStore
from .pairing import PairingStore
from .runner import REFUSED_EXIT_STATUS
from .sessions import SessionStore
from .snapshots import SnapshotStore
from .transcripts import TranscriptStore

BACKUPS_DIRNAME = "backups"
KEEP_BACKUPS = 3
BACKUP_EVERY_SECONDS = 24 * 3600.0
# How often the running host controller looks for a backup that's due.
_BACKUP_CHECK_SECONDS = 3600.0
_STAMP = "%Y%m%dT%H%M%SZ"
_PREFIX, _SUFFIX = "bot-bottle-", ".db"


class HostDbError(Exception):
    """The host DB can't be checked, backed up or restored as asked."""


def stores(db_path: Path | None = None) -> list[DbStore]:
    """Every store with tables in the host DB. Most migrate as they open."""
    path = db_path or host_db_path()
    return [ConfigStore(path), ForwardCursors(path), ForgeStore(path), TrustStore(path),
            PairingStore(path), PasskeyStore(path), BottledAgentStore(path), SessionStore(path),
            AuditPushStore(path),
            AgentPrefsStore(path), TranscriptStore(path), SnapshotStore(path),
            IsolationPasses(path), InfraStateStore(path), RunManifestStore(path),
            WorkspaceDiskStore(path)]


def migrate(db_path: Path | None = None) -> list[str]:
    """Bring every host table up to date, and drop every table no host store
    owns: what an older version kept here (its audit index, or on Docker the
    orchestrator's tables with their encrypted secrets and identity tokens),
    so neither the DB nor its backups carry it. Returns the tables dropped."""
    path = db_path or host_db_path()
    owners = stores(path)
    for store in owners:
        store.migrate()
    dropped = drop_unowned(path, [t for s in owners for t in s.TABLES],
                           [s.schema_key for s in owners])
    if dropped:
        log.info(f"host database: dropped tables no host store owns: {', '.join(dropped)}")
    return dropped


def check(db_path: Path | None = None) -> str:
    """The first problem `PRAGMA quick_check` finds in the DB, or "" if it
    passes (or there's no file yet)."""
    return quick_check(db_path or host_db_path())


@dataclass(frozen=True)
class Backup:
    path: Path
    taken: datetime  # UTC


def backups_dir(db_path: Path | None = None) -> Path:
    return (db_path or host_db_path()).parent / BACKUPS_DIRNAME


def backups(db_path: Path | None = None) -> list[Backup]:
    """The backups, newest first."""
    found: list[Backup] = []
    directory = backups_dir(db_path)
    for path in directory.glob(f"{_PREFIX}*{_SUFFIX}") if directory.is_dir() else ():
        try:
            taken = datetime.strptime(path.name[len(_PREFIX):-len(_SUFFIX)], _STAMP)
        except ValueError:
            continue
        found.append(Backup(path, taken.replace(tzinfo=timezone.utc)))
    return sorted(found, key=lambda b: b.taken, reverse=True)


def _private_dir(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise HostDbError(f"backups must be a directory: {directory}")
    directory.chmod(0o700)


def _private_file(path: Path) -> None:
    """Create `path` empty and 0600, so nothing written to it is ever readable
    by others."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 stat.S_IRUSR | stat.S_IWUSR)
    os.close(fd)


def back_up(db_path: Path | None = None, now: datetime | None = None) -> Path:
    """Copy the DB into a new backup (`VACUUM INTO`, consistent while it's in
    use) and keep the newest `KEEP_BACKUPS`. Returns the backup."""
    path = db_path or host_db_path()
    directory = backups_dir(path)
    _private_dir(directory)
    when = (now or datetime.now(timezone.utc)).strftime(_STAMP)
    target = directory / f"{_PREFIX}{when}{_SUFFIX}"
    partial = directory / f".{target.name}.partial"
    partial.unlink(missing_ok=True)
    _private_file(partial)
    try:
        with closing(connect(path)) as conn:
            conn.execute("VACUUM INTO ?", (str(partial),))
        partial.chmod(0o600)
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    for old in backups(path)[KEEP_BACKUPS:]:
        old.path.unlink(missing_ok=True)
    return target


def back_up_if_due(db_path: Path | None = None, now: datetime | None = None) -> Path | None:
    """Take a backup if the newest is a day old or there's none, and the DB
    exists. Returns the new backup, if one was taken."""
    path = db_path or host_db_path()
    now = now or datetime.now(timezone.utc)
    newest = next(iter(backups(path)), None)
    if not path.exists() or (
            newest is not None and (now - newest.taken).total_seconds() < BACKUP_EVERY_SECONDS):
        return None
    return back_up(path, now)


def refusal(problem: str, db_path: Path | None = None) -> str:
    """Why the host controller won't start on a DB that failed its check, and
    how to restore. One line: the CLI shows the log's last."""
    path = db_path or host_db_path()
    newest = next(iter(backups(path)), None)
    if newest is None:
        restore_hint = ("there's no backup to restore; move the file aside to start with an "
                        "empty host database")
    else:
        restore_hint = (f"the newest backup is {newest.path.name}, taken "
                        f"{newest.taken:%Y-%m-%d %H:%M} UTC (changes since then would be lost); "
                        "restore it with `bot-bottle host restore-db`")
    return f"host database {path} failed its integrity check ({problem}): {restore_hint}"


@dataclass(frozen=True)
class Restored:
    db: Path
    backup: Path
    # Where the replaced DB was moved, if there was one.
    replaced: Path | None


def restore(backup: Path | None = None, db_path: Path | None = None) -> Restored:
    """Put `backup` (default: the newest) back as the host DB. The DB it
    replaces is kept beside it. Only while the host controller is stopped."""
    path = db_path or host_db_path()
    if backup is None:
        newest = next(iter(backups(path)), None)
        if newest is None:
            raise HostDbError(f"no backups in {backups_dir(path)}")
        backup = newest.path
    if not backup.is_file():
        raise HostDbError(f"no backup at {backup}")
    problem = check(backup)
    if problem:
        raise HostDbError(f"{backup} fails its own check ({problem})")
    staged = path.with_name(f".{path.name}.restoring")
    staged.unlink(missing_ok=True)
    _private_file(staged)
    with backup.open("rb") as src, staged.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    replaced = None
    if path.exists():
        replaced = path.with_name(
            f"{path.name}.replaced-{datetime.now(timezone.utc).strftime(_STAMP)}")
        os.replace(path, replaced)
    # The old DB's WAL belongs to it, not to the restored copy.
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            if replaced is None:
                side.unlink()
            else:
                os.replace(side, replaced.with_name(replaced.name + suffix))
    os.replace(staged, path)
    return Restored(db=path, backup=backup, replaced=replaced)


class DailyBackups:
    """The running host controller's daily backup, checked hourly."""

    def __init__(self, interval: float = _BACKUP_CHECK_SECONDS) -> None:
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="hostctl-db-backups")

    def start(self) -> "DailyBackups":
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Stop, waiting for a backup in progress."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                back_up_if_due()
            except (OSError, sqlite3.Error, HostDbError) as e:
                log.warn(f"host DB backup failed: {e}")


def open_for_start() -> bool:
    """The host controller's start: check the DB, migrate it, take the day's
    backup. False (and why, logged) when the check fails."""
    problem = check()
    if problem:
        log.error(refusal(problem))
        return False
    # Migrated first, so the backup holds nothing `migrate` drops.
    migrate()
    try:
        back_up_if_due()
    except (OSError, sqlite3.Error, HostDbError) as e:
        log.warn(f"host DB backup failed: {e}")
    return True


__all__ = [
    "BACKUPS_DIRNAME", "KEEP_BACKUPS", "REFUSED_EXIT_STATUS", "Backup", "DailyBackups",
    "HostDbError", "Restored", "back_up", "back_up_if_due", "backups", "backups_dir", "check",
    "migrate", "open_for_start", "refusal", "restore", "stores",
]
