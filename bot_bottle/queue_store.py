"""SQLite-backed queue store for supervise proposals and responses (PRD 0013)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

try:
    from .supervise_types import Proposal, Response
    from .paths import host_db_path
    from .db_store import DbStore
    from .migrations import TableMigrations
except ImportError:
    from supervise_types import Proposal, Response  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module
    from paths import host_db_path  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module
    from db_store import DbStore  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module
    from migrations import TableMigrations  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module


class QueueStore(DbStore):
    """SQLite-backed persistent store for supervise proposals and responses."""

    def __init__(self, queue_key: str, db_path: Path | None = None) -> None:
        self.queue_key = queue_key
        if db_path is not None:
            resolved = db_path
        else:
            # In the sidecar container SUPERVISE_DB_PATH points at the
            # bind-mounted host DB. On the host this env var is never set,
            # so we always fall through to host_db_path().
            env_path = os.environ.get("SUPERVISE_DB_PATH", "").strip()
            resolved = Path(env_path) if env_path else host_db_path()
        # One entry per schema version: migrations[0] brings a fresh DB to
        # version 1, [1] to version 2, etc.  Add new entries at the end; never
        # edit existing ones.
        migrations = TableMigrations("queue_store", [
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
        ])
        super().__init__(resolved, migrations)

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
        return Proposal(
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
        return Response(
            proposal_id=row["proposal_id"],
            status=row["status"],
            notes=row["notes"],
            final_file=row["final_file"],
        )


__all__ = ["QueueStore"]
