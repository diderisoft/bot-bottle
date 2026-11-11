"""The bottle registry: each live bottle's source IP and identity token, and
fail-closed attribution (both must match one active record), in the
orchestrator's `registry.db`.

The source IP is the network-layer signal (unspoofable on firecracker, weaker
elsewhere); the identity token is an application-layer secret a bottle can't
learn for another.
"""

from __future__ import annotations

import hmac
import secrets
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ...lib.db_store import MACHINE_SPECIFIC, DbStore
from ...lib.migrations import TableMigrations

# 256 bits, URL-safe.
IDENTITY_TOKEN_BYTES = 32

# Rows newer than this are exempt from `reap_absent`, covering a launch whose
# address isn't visible yet.
DEFAULT_REAP_GRACE_SECONDS = 120.0


def new_identity_token() -> str:
    """A fresh per-bottle identity token."""
    return secrets.token_urlsafe(IDENTITY_TOKEN_BYTES)


@dataclass(frozen=True)
class BottleRecord:
    """One live bottle in the registry."""

    bottle_id: str
    source_ip: str
    identity_token: str
    state: str = "active"
    created_at: float = 0.0
    # Opaque JSON, so new fields need no migration.
    metadata: str = ""
    # The bottle's gateway policy, opaque JSON stored and served verbatim.
    policy: str = ""

    def redacted(self) -> dict[str, object]:
        """Listing view; never includes the identity token."""
        return {
            "bottle_id": self.bottle_id,
            "source_ip": self.source_ip,
            "state": self.state,
            "created_at": self.created_at,
            "metadata": self.metadata,
            "policy": self.policy,
        }


_MIGRATIONS = TableMigrations(
    "orchestrator_registry",
    [
        # v1: the registry.
        """
        CREATE TABLE IF NOT EXISTS orchestrator_bottles (
            bottle_id      TEXT PRIMARY KEY,
            source_ip      TEXT NOT NULL,
            identity_token TEXT NOT NULL,
            state          TEXT NOT NULL DEFAULT 'active',
            created_at     REAL NOT NULL,
            metadata       TEXT NOT NULL DEFAULT ''
        )
        """,
        # The attribution key; indexed.
        "CREATE INDEX IF NOT EXISTS idx_orchestrator_bottles_source_ip "
        "ON orchestrator_bottles (source_ip)",
        # v3: per-bottle policy.
        "ALTER TABLE orchestrator_bottles ADD COLUMN policy TEXT NOT NULL DEFAULT ''",
        # v4: encrypted egress secrets, one row per env var (name in
        # plaintext). The key lives only in the agent's env.
        """
        CREATE TABLE IF NOT EXISTS bottled_agent_secrets (
            bottled_agent_id TEXT NOT NULL,
            key              TEXT NOT NULL,
            value            TEXT NOT NULL,
            type             TEXT NOT NULL DEFAULT 'injected_env_var'
        )
        """,
        # v5: per-bottle index.
        "CREATE INDEX IF NOT EXISTS idx_bottled_agent_secrets_id "
        "ON bottled_agent_secrets (bottled_agent_id, type)",
        # v6: drop unauthenticated legacy ciphertext; affected bottles
        # reprovision.
        "DELETE FROM bottled_agent_secrets",
    ],
)


def _row_to_record(row: sqlite3.Row) -> BottleRecord:
    """Build a BottleRecord from a registry row."""
    return BottleRecord(
        bottle_id=row["bottle_id"],
        source_ip=row["source_ip"],
        identity_token=row["identity_token"],
        state=row["state"],
        created_at=row["created_at"],
        metadata=row["metadata"],
        policy=row["policy"],
    )


class RegistryStore(DbStore):
    """SQLite-backed registry of live bottles + fail-closed attribution."""

    TABLES = {"orchestrator_bottles": MACHINE_SPECIFIC,
              "bottled_agent_secrets": MACHINE_SPECIFIC}

    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path, _MIGRATIONS)

    def register(
        self,
        source_ip: str,
        *,
        bottle_id: str | None = None,
        identity_token: str | None = None,
        metadata: str = "",
        policy: str = "",
    ) -> BottleRecord:
        """Register or replace a bottle, minting its id and token if not given.
        Other active rows at the same source IP are removed first, since an
        ambiguous IP attributes to nothing."""
        rec = BottleRecord(
            bottle_id=bottle_id or secrets.token_hex(8),
            source_ip=source_ip,
            identity_token=identity_token or new_identity_token(),
            state="active",
            created_at=time.time(),
            metadata=metadata,
            policy=policy,
        )
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM orchestrator_bottles "
                "WHERE source_ip = ? AND state = 'active' AND bottle_id != ?",
                (rec.source_ip, rec.bottle_id),
            )
            conn.execute(
                "INSERT OR REPLACE INTO orchestrator_bottles "
                "(bottle_id, source_ip, identity_token, state, created_at, metadata, policy) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    rec.bottle_id,
                    rec.source_ip,
                    rec.identity_token,
                    rec.state,
                    rec.created_at,
                    rec.metadata,
                    rec.policy,
                ),
            )
        self._chmod()
        return rec

    def set_policy(self, bottle_id: str, policy: str) -> bool:
        """Replace a bottle's policy. True if it exists."""
        with self._connection() as conn:
            cur = conn.execute(
                "UPDATE orchestrator_bottles SET policy = ? WHERE bottle_id = ?",
                (policy, bottle_id),
            )
        self._chmod()
        return cur.rowcount > 0

    def deregister(self, bottle_id: str) -> bool:
        """Remove a bottle. Returns True if a row was deleted."""
        with self._connection() as conn:
            cur = conn.execute(
                "DELETE FROM orchestrator_bottles WHERE bottle_id = ?", (bottle_id,)
            )
            return cur.rowcount > 0

    def get(self, bottle_id: str) -> BottleRecord | None:
        """Return the bottle by id, or None if absent."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM orchestrator_bottles WHERE bottle_id = ?", (bottle_id,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def all(self) -> list[BottleRecord]:
        """Every registered bottle, oldest first."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM orchestrator_bottles ORDER BY created_at"
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def reap_absent(
        self,
        live_source_ips: Iterable[str],
        *,
        grace_seconds: float = DEFAULT_REAP_GRACE_SECONDS,
        now: float | None = None,
    ) -> list[BottleRecord]:
        """Delete active rows whose source IP no live bottle holds, leaving at
        most one row per live address.

        Rows outlive a hard-killed launcher, and a recycled address with a
        stale row resolves no policy for the next bottle. Two cases: no live
        bottle at the address (all rows are orphans), or several rows at a live
        address (the newest wins). Rows younger than `grace_seconds` are kept
        so an in-flight launch isn't raced. Returns the deleted records."""
        live = {ip for ip in live_source_ips if ip}
        cutoff = (time.time() if now is None else now) - grace_seconds
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM orchestrator_bottles WHERE state = 'active'",
            ).fetchall()
            by_ip: dict[str, list[BottleRecord]] = {}
            for row in rows:
                rec = _row_to_record(row)
                by_ip.setdefault(rec.source_ip, []).append(rec)
            candidates: list[BottleRecord] = []
            for ip, recs in by_ip.items():
                if ip not in live:
                    candidates.extend(recs)
                    continue
                # Keep the newest claim; drop the rest.
                recs.sort(key=lambda r: r.created_at)
                candidates.extend(recs[:-1])
            doomed = [r for r in candidates if r.created_at <= cutoff]
            for rec in doomed:
                conn.execute(
                    "DELETE FROM orchestrator_bottles WHERE bottle_id = ?",
                    (rec.bottle_id,),
                )
        if doomed:
            self._chmod()
        return doomed

    def by_source_ip(self, source_ip: str) -> BottleRecord | None:
        """The single active bottle at this source IP, or None if unknown or
        ambiguous. Pair with the identity token (`attribute`) unless the IP is
        unspoofable."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM orchestrator_bottles "
                "WHERE source_ip = ? AND state = 'active'",
                (source_ip,),
            ).fetchall()
        if len(rows) != 1:
            return None
        return _row_to_record(rows[0])

    def attribute(self, source_ip: str, identity_token: str) -> BottleRecord | None:
        """Fail-closed attribution: `by_source_ip` and a matching token
        (constant-time)."""
        if not identity_token:
            return None
        rec = self.by_source_ip(source_ip)
        if rec is None:
            return None
        if not hmac.compare_digest(rec.identity_token, identity_token):
            return None
        return rec

    # --- encrypted egress secrets ---

    def store_agent_secrets(
        self,
        bottle_id: str,
        encrypted_values: dict[str, str],
        secret_type: str = "injected_env_var",
    ) -> None:
        """Replace `bottle_id`'s stored secrets with `encrypted_values` (env
        name -> ciphertext)."""
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM bottled_agent_secrets "
                "WHERE bottled_agent_id = ? AND type = ?",
                (bottle_id, secret_type),
            )
            conn.executemany(
                "INSERT INTO bottled_agent_secrets "
                "(bottled_agent_id, key, value, type) VALUES (?, ?, ?, ?)",
                [(bottle_id, k, v, secret_type) for k, v in encrypted_values.items()],
            )
        self._chmod()

    def store_agent_secret(
        self,
        bottle_id: str,
        key: str,
        encrypted_value: str,
        secret_type: str = "injected_env_var",
    ) -> None:
        """Upsert one encrypted row for `bottle_id`, leaving the others."""
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM bottled_agent_secrets "
                "WHERE bottled_agent_id = ? AND key = ? AND type = ?",
                (bottle_id, key, secret_type),
            )
            conn.execute(
                "INSERT INTO bottled_agent_secrets "
                "(bottled_agent_id, key, value, type) VALUES (?, ?, ?, ?)",
                (bottle_id, key, encrypted_value, secret_type),
            )
        self._chmod()

    def get_agent_secrets(
        self,
        bottle_id: str,
        secret_type: str = "injected_env_var",
    ) -> dict[str, str]:
        """{env name: ciphertext} for `bottle_id`, or {}."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT key, value FROM bottled_agent_secrets "
                "WHERE bottled_agent_id = ? AND type = ?",
                (bottle_id, secret_type),
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def delete_agent_secrets(
        self,
        bottle_id: str,
        secret_type: str = "injected_env_var",
    ) -> None:
        """Remove `bottle_id`'s stored secrets."""
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM bottled_agent_secrets "
                "WHERE bottled_agent_id = ? AND type = ?",
                (bottle_id, secret_type),
            )


__all__ = [
    "BottleRecord",
    "RegistryStore",
    "new_identity_token",
    "IDENTITY_TOKEN_BYTES",
    "DEFAULT_REAP_GRACE_SECONDS",
]
