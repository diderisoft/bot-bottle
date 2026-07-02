"""SQLite-backed queue store for supervise proposals and responses (PRD 0013)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .supervise import Proposal, Response


def get_supervise_mod() -> object:
    """Lazy import of supervise to avoid a circular-import at module init time.
    By the time any QueueStore method is called, both modules are fully loaded.

    Mirrors our own module identity: when we are 'queue_store' (sidecar flat
    context or tests that inject bot_bottle/ into sys.path) we use the flat
    'supervise' module so that patches on supervise.bot_bottle_root propagate
    correctly. When we are 'bot_bottle.queue_store' we use 'bot_bottle.supervise'."""
    import sys
    sv_name = "supervise" if __name__ == "queue_store" else "bot_bottle.supervise"
    if sv_name in sys.modules:
        return sys.modules[sv_name]
    try:
        import bot_bottle.supervise as _m
    except ImportError:
        import supervise as _m  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module
    return _m


# One entry per schema version: _MIGRATIONS[0] brings a fresh DB (user_version=0)
# to version 1, _MIGRATIONS[1] to version 2, and so on.  Add new migrations at
# the end; never edit existing ones.
_MIGRATIONS: list[str] = [
    # v1 — proposals table
    """
    CREATE TABLE IF NOT EXISTS supervise_proposals (
        queue_key TEXT NOT NULL,
        id TEXT NOT NULL,
        bottle_slug TEXT NOT NULL,
        tool TEXT NOT NULL,
        proposed_file TEXT NOT NULL,
        justification TEXT NOT NULL,
        arrival_timestamp TEXT NOT NULL,
        current_file_hash TEXT NOT NULL,
        archived INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (queue_key, id)
    )
    """,
    # v2 — responses table
    """
    CREATE TABLE IF NOT EXISTS supervise_responses (
        queue_key TEXT NOT NULL,
        proposal_id TEXT NOT NULL,
        status TEXT NOT NULL,
        notes TEXT NOT NULL,
        final_file TEXT,
        archived INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (queue_key, proposal_id)
    )
    """,
]


class QueueStore:
    """SQLite-backed persistent store for supervise proposals and responses."""

    def __init__(self, queue_key: str, db_path: Path | None = None) -> None:
        self.queue_key = queue_key
        if db_path is not None:
            self.db_path = db_path
        else:
            # In the sidecar container SUPERVISE_DB_PATH points at the
            # bind-mounted host DB. On the host this env var is never set,
            # so we always fall through to host_db_path().
            env_path = os.environ.get("SUPERVISE_DB_PATH", "").strip()
            self.db_path = Path(env_path) if env_path else get_supervise_mod().host_db_path()  # type: ignore[attr-defined]
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def write_proposal(self, proposal: Proposal) -> Path:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO supervise_proposals (
                    queue_key, id, bottle_slug, tool, proposed_file, justification,
                    arrival_timestamp, current_file_hash, archived
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    self.queue_key,
                    proposal.id,
                    proposal.bottle_slug,
                    proposal.tool,
                    proposal.proposed_file,
                    proposal.justification,
                    proposal.arrival_timestamp,
                    proposal.current_file_hash,
                ),
            )
        self._chmod()
        return self.db_path

    def read_proposal(self, proposal_id: str) -> Proposal:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM supervise_proposals
                WHERE queue_key = ? AND id = ? AND archived = 0
                """,
                (self.queue_key, proposal_id),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(proposal_id)
        return self._row_to_proposal(row)

    def list_pending_proposals(self) -> list[Proposal]:
        if not self.db_path.is_file():
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT p.* FROM supervise_proposals p
                WHERE p.archived = 0
                  AND p.queue_key = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM supervise_responses r
                    WHERE r.queue_key = p.queue_key
                      AND r.proposal_id = p.id
                      AND r.archived = 0
                  )
                ORDER BY p.arrival_timestamp, p.id
                """,
                (self.queue_key,),
            ).fetchall()
        return [self._row_to_proposal(row) for row in rows]

    def list_all_pending_proposals(self) -> list[Proposal]:
        if not self.db_path.is_file():
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT p.* FROM supervise_proposals p
                WHERE p.archived = 0
                  AND NOT EXISTS (
                    SELECT 1 FROM supervise_responses r
                    WHERE r.queue_key = p.queue_key
                      AND r.proposal_id = p.id
                      AND r.archived = 0
                  )
                ORDER BY p.arrival_timestamp, p.id
                """
            ).fetchall()
        return [self._row_to_proposal(row) for row in rows]

    def write_response(self, response: Response) -> Path:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO supervise_responses (
                    queue_key, proposal_id, status, notes, final_file, archived
                ) VALUES (?, ?, ?, ?, ?, 0)
                """,
                (
                    self.queue_key,
                    response.proposal_id,
                    response.status,
                    response.notes,
                    response.final_file,
                ),
            )
        self._chmod()
        return self.db_path

    def read_response(self, proposal_id: str) -> Response:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM supervise_responses
                WHERE queue_key = ? AND proposal_id = ? AND archived = 0
                """,
                (self.queue_key, proposal_id),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(proposal_id)
        return self._row_to_response(row)

    def archive_proposal(self, proposal_id: str) -> None:
        if not self.db_path.is_file():
            return
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE supervise_proposals SET archived = 1
                WHERE queue_key = ? AND id = ?
                """,
                (self.queue_key, proposal_id),
            )
            conn.execute(
                """
                UPDATE supervise_responses SET archived = 1
                WHERE queue_key = ? AND proposal_id = ?
                """,
                (self.queue_key, proposal_id),
            )

    @staticmethod
    def _row_to_proposal(row: sqlite3.Row) -> Proposal:
        m = get_supervise_mod()
        return m.Proposal(  # type: ignore[attr-defined]
            id=row["id"],
            bottle_slug=row["bottle_slug"],
            tool=row["tool"],
            proposed_file=row["proposed_file"],
            justification=row["justification"],
            arrival_timestamp=row["arrival_timestamp"],
            current_file_hash=row["current_file_hash"],
        )

    @staticmethod
    def _row_to_response(row: sqlite3.Row) -> Response:
        m = get_supervise_mod()
        return m.Response(  # type: ignore[attr-defined]
            proposal_id=row["proposal_id"],
            status=row["status"],
            notes=row["notes"],
            final_file=row["final_file"],
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    _SCHEMA_KEY = "queue_store"

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_versions (
                    module TEXT PRIMARY KEY,
                    version INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            row = conn.execute(
                "SELECT version FROM schema_versions WHERE module = ?",
                (self._SCHEMA_KEY,),
            ).fetchone()
            version = row[0] if row else 0
            for i, sql in enumerate(_MIGRATIONS[version:], start=version + 1):
                conn.execute(sql)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_versions (module, version) VALUES (?, ?)",
                    (self._SCHEMA_KEY, i),
                )
        self._chmod()

    def _chmod(self) -> None:
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass


__all__ = ["QueueStore"]
