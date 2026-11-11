"""SQLite store for supervise proposals and responses: working state, deleted
when the bottle goes. The journal is their record (`decision.*`)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ...contract.supervise import Proposal, Response
from ...lib.db_store import MACHINE_SPECIFIC, DbStore
from ...lib.migrations import TableMigrations


class QueueStore(DbStore):
    TABLES = {"supervise_proposals": MACHINE_SPECIFIC,
              "supervise_responses": MACHINE_SPECIFIC}


    def __init__(self, queue_key: str, db_path: Path) -> None:
        self.queue_key = queue_key
        # One migration per schema version; append only.
        migrations = TableMigrations("queue_store", [
            # v1: proposals
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
            # v2: responses
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
            # v3-6: a gone bottle's proposals and answers are deleted, not
            # archived: the journal records them (`decision.*`).
            "DELETE FROM supervise_proposals WHERE archived = 1",
            "ALTER TABLE supervise_proposals DROP COLUMN archived",
            "DELETE FROM supervise_responses WHERE archived = 1",
            "ALTER TABLE supervise_responses DROP COLUMN archived",
        ])
        super().__init__(db_path, migrations)

    def write_proposal(self, proposal: Proposal) -> Path:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO supervise_proposals (
                    queue_key, id, bottle_slug, tool, proposed_file, justification,
                    arrival_timestamp, current_file_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM supervise_proposals
                WHERE queue_key = ? AND id = ?
                """,
                (self.queue_key, proposal_id),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(proposal_id)
        return self._row_to_proposal(row)

    def list_pending_proposals(self) -> list[Proposal]:
        if not self.db_path.is_file():
            return []
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT p.* FROM supervise_proposals p
                WHERE p.queue_key = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM supervise_responses r
                    WHERE r.queue_key = p.queue_key
                      AND r.proposal_id = p.id
                  )
                ORDER BY p.arrival_timestamp, p.id
                """,
                (self.queue_key,),
            ).fetchall()
        return [self._row_to_proposal(row) for row in rows]

    def list_all_pending_proposals(self) -> list[Proposal]:
        if not self.db_path.is_file():
            return []
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT p.* FROM supervise_proposals p
                WHERE NOT EXISTS (
                    SELECT 1 FROM supervise_responses r
                    WHERE r.queue_key = p.queue_key
                      AND r.proposal_id = p.id
                  )
                ORDER BY p.arrival_timestamp, p.id
                """
            ).fetchall()
        return [self._row_to_proposal(row) for row in rows]

    def write_response(self, response: Response) -> Path:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO supervise_responses (
                    queue_key, proposal_id, status, notes, final_file
                ) VALUES (?, ?, ?, ?, ?)
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
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM supervise_responses
                WHERE queue_key = ? AND proposal_id = ?
                """,
                (self.queue_key, proposal_id),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(proposal_id)
        return self._row_to_response(row)

    def delete_all(self) -> None:
        """Delete every proposal and response for this key (bottle teardown or
        reconcile). The journal is their record. Idempotent."""
        if not self.db_path.is_file():
            return
        with self._connection() as conn:
            conn.execute("DELETE FROM supervise_proposals WHERE queue_key = ?",
                         (self.queue_key,))
            conn.execute("DELETE FROM supervise_responses WHERE queue_key = ?",
                         (self.queue_key,))

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
