"""A library conflict this host is waiting on (PRD 0131), in the host DB
(`library_conflict`), so it outlives a host controller restart.

The console says this host's branch didn't merge (`library_conflict`); until
its `main` contains the commit this host had then (`tip`), the console holds
the conflict and this host keeps its own version. `library_link` sets and
clears it; CLI launches and `bot-bottle status` say it's pending.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from ...lib.db_store import MACHINE_SPECIFIC, DbStore
from ...lib.migrations import TableMigrations


@dataclass(frozen=True)
class Conflict:
    files: tuple[str, ...]  # what the console said conflicts
    since: float  # time.time() of the first report
    tip: str  # this host's HEAD then: resolved once `main` contains it

    def to_json(self) -> dict[str, object]:
        return {"files": list(self.files), "since": self.since, "tip": self.tip}


class LibraryConflictStore(DbStore):
    """`library_conflict`: the one pending conflict, or no row."""

    # This host's branch on the account's library.
    TABLES = {"library_conflict": MACHINE_SPECIFIC}

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, TableMigrations("library_conflict", [
            """
            CREATE TABLE library_conflict (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                files TEXT NOT NULL,
                since REAL NOT NULL,
                tip TEXT NOT NULL DEFAULT ''
            )
            """,
        ]))
        self.migrate()

    def pending(self) -> Conflict | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM library_conflict WHERE id = 1").fetchone()
        if row is None:
            return None
        try:
            files = json.loads(row["files"])
        except ValueError:
            files = []
        return Conflict(files=tuple(str(f) for f in files) if isinstance(files, list) else (),
                        since=float(row["since"]), tip=row["tip"])

    def report(self, files: list[str], tip: str) -> Conflict:
        """The console reported a conflict: keep `files` and `tip`, and when
        the conflict was first reported if one is already pending."""
        with self._write_connection() as conn:
            row = conn.execute("SELECT since FROM library_conflict WHERE id = 1").fetchone()
            since = float(row["since"]) if row is not None else time.time()
            conn.execute("INSERT OR REPLACE INTO library_conflict VALUES (1, ?, ?, ?)",
                         (json.dumps(files), since, tip))
        self._chmod()
        return Conflict(files=tuple(files), since=since, tip=tip)

    def clear(self) -> bool:
        """Forget the pending conflict; whether there was one."""
        with self._connection() as conn:
            cleared = conn.execute("DELETE FROM library_conflict").rowcount > 0
        self._chmod()
        return cleared


__all__ = ["Conflict", "LibraryConflictStore"]
