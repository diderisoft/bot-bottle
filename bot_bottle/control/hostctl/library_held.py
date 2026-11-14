"""Library files held back from sync because they don't parse (PRD 0131), in
the host DB (`library_held`), so the hold outlives a host controller restart.

A host commits a hand edit that doesn't parse as usual, but doesn't push
while any file changed since its last push doesn't parse at its `HEAD`
(`unparseable`), so a half-written file never reaches every host. The console
hears of it (`library_held`), and `bot-bottle status` says so, until they
parse again and the host pushes.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...lib.db_store import MACHINE_SPECIFIC, DbStore
from ...lib.migrations import TableMigrations
from ...manifest import parse_check

# The longest error kept for a file: the console shows it on one line.
MAX_ERROR = 300


@dataclass(frozen=True)
class HeldFile:
    path: str  # relative to the library's root, e.g. `bottles/dev.md`
    error: str  # the parser's message

    def to_json(self) -> dict[str, str]:
        return {"path": self.path, "error": self.error}


@dataclass(frozen=True)
class Held:
    files: tuple[HeldFile, ...]
    since: float  # time.time() when the hold began


def unparseable(paths: list[str], read: Callable[[str], str | None]) -> tuple[HeldFile, ...]:
    """The bottle and agent files among `paths` that don't parse, as `read`
    gives them (None: deleted, which is fine)."""
    held = []
    for rel in sorted(set(paths)):
        if not parse_check.checked(rel):
            continue
        text = read(rel)
        error = parse_check.problem(rel, text) if text is not None else ""
        if error:
            short = " ".join(error.split())
            held.append(HeldFile(rel, short if len(short) <= MAX_ERROR
                                 else short[:MAX_ERROR - 1] + "…"))
    return tuple(held)


class LibraryHeldStore(DbStore):
    """`library_held`: the files held back, or no row."""

    # This host's working tree.
    TABLES = {"library_held": MACHINE_SPECIFIC}

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, TableMigrations("library_held", [
            """
            CREATE TABLE library_held (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                files TEXT NOT NULL,
                since REAL NOT NULL
            )
            """,
        ]))
        self.migrate()

    def held(self) -> Held | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM library_held WHERE id = 1").fetchone()
        if row is None:
            return None
        try:
            raw = json.loads(row["files"])
        except ValueError:
            raw = []
        files = tuple(HeldFile(str(f.get("path", "")), str(f.get("error", "")))
                      for f in (raw if isinstance(raw, list) else []) if isinstance(f, dict))
        return Held(files=files, since=float(row["since"]))

    def hold(self, files: tuple[HeldFile, ...]) -> Held:
        """Hold `files` back, since the hold began if one is already on."""
        with self._write_connection() as conn:
            row = conn.execute("SELECT since FROM library_held WHERE id = 1").fetchone()
            since = float(row["since"]) if row is not None else time.time()
            conn.execute("INSERT OR REPLACE INTO library_held VALUES (1, ?, ?)",
                         (json.dumps([f.to_json() for f in files]), since))
        self._chmod()
        return Held(files=files, since=since)

    def clear(self) -> bool:
        """Nothing's held anymore; whether something was."""
        with self._connection() as conn:
            cleared = conn.execute("DELETE FROM library_held").rowcount > 0
        self._chmod()
        return cleared


__all__ = ["Held", "HeldFile", "LibraryHeldStore", "MAX_ERROR", "unparseable"]
