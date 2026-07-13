"""Orchestrator bottle registry — the per-host runtime-state store (PRD 0070).

SQLite-backed registry mapping each live bottle to its source IP and
per-bottle identity token, plus the **fail-closed attribution** the data
plane relies on: a request is attributed to a bottle only when its source
IP *and* its identity token both match a single active record.

The registry co-tenants the shared host `bot-bottle.db` (the `DbStore`
framework namespaces each store by `schema_key`), so all bot-bottle
runtime state lives in one queryable file — one place to back up, inspect,
and integrate a console against.

This is the "runtime state" tier of PRD 0070 (leases / approvals /
registry) — deliberately NOT config (which stays declarative under
`~/.bot-bottle/`) and NOT the build-time constants (a flat file). It is the
source of truth the orchestrator sweeps on restart to re-adopt running
bottles, so it lives in a durable store rather than process memory.

Attribution combines two independent signals, and needs both:

  * source IP  — the network-layer invariant (on Firecracker the `/31` TAP
    + nft make it unspoofable by construction; weaker on other backends).
  * identity token — an application-layer per-bottle secret, defence in
    depth that doesn't lean on network anti-spoof. A bottle can only ever
    prove it is *itself* (it can't learn another bottle's token), so a
    hostile agent gains nothing by presenting it.
"""

from __future__ import annotations

import hmac
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from ..db_store import DbStore
from ..migrations import TableMigrations
from ..paths import host_db_path

# 256 bits of urandom, URL-safe — unguessable per-bottle identity token.
IDENTITY_TOKEN_BYTES = 32


def new_identity_token() -> str:
    """A fresh per-bottle identity token (PRD 0070 attribution defence)."""
    return secrets.token_urlsafe(IDENTITY_TOKEN_BYTES)


def default_db_path() -> Path:
    """The shared host state DB (`bot-bottle.db`) — all bot-bottle runtime
    state in one file, co-tenanted via schema_key. Host-resident so it
    survives orchestrator restarts (re-adoption sweeps it)."""
    return host_db_path()


@dataclass(frozen=True)
class BottleRecord:
    """One live bottle in the registry."""

    bottle_id: str
    source_ip: str
    identity_token: str
    state: str = "active"
    created_at: float = 0.0
    # Opaque JSON blob for forward-compat (pool slot, ...) — the registry
    # doesn't interpret it, so new fields need no migration.
    metadata: str = ""
    # The bottle's gateway policy (opaque JSON — egress allowlist / routes /
    # git config). The registry stores and serves it verbatim, keyed by
    # source IP; the multi-tenant gateway interprets it.
    policy: str = ""

    def redacted(self) -> dict[str, object]:
        """Public view for listing — never exposes the identity token."""
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
        # v1 — the live bottle registry.
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
        # source_ip is the data-plane attribution key — index it, and make
        # the "one active bottle per source IP" check cheap.
        "CREATE INDEX IF NOT EXISTS idx_orchestrator_bottles_source_ip "
        "ON orchestrator_bottles (source_ip)",
        # v3 — per-bottle policy (opaque JSON the gateway interprets): the
        # egress allowlist / routes / git config selected by source IP. The
        # multi-tenant gateway resolves it per request via `attribute`.
        "ALTER TABLE orchestrator_bottles ADD COLUMN policy TEXT NOT NULL DEFAULT ''",
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

    def __init__(self, db_path: Path | None = None) -> None:
        super().__init__(db_path or default_db_path(), _MIGRATIONS)

    def _connect(self) -> sqlite3.Connection:
        """Open a connection with a busy timeout for the shared DB."""
        conn = super()._connect()
        # The registry co-tenants the shared bot-bottle.db, so a busy_timeout
        # rides out brief lock contention with the other stores. WAL for the
        # shared DB is a deliberate future change (it affects supervise/audit
        # and is finicky over guest shares) — not flipped here.
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def register(
        self,
        source_ip: str,
        *,
        bottle_id: str | None = None,
        identity_token: str | None = None,
        metadata: str = "",
        policy: str = "",
    ) -> BottleRecord:
        """Register (or replace) a bottle. Mints a bottle_id / identity token
        when not supplied. Live — this is the control-plane reload path."""
        rec = BottleRecord(
            bottle_id=bottle_id or secrets.token_hex(8),
            source_ip=source_ip,
            identity_token=identity_token or new_identity_token(),
            state="active",
            created_at=time.time(),
            metadata=metadata,
            policy=policy,
        )
        with self._connect() as conn:
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
        """Update a bottle's policy in place (live reload). Returns True if
        the bottle exists."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE orchestrator_bottles SET policy = ? WHERE bottle_id = ?",
                (policy, bottle_id),
            )
        self._chmod()
        return cur.rowcount > 0

    def deregister(self, bottle_id: str) -> bool:
        """Remove a bottle. Returns True if a row was deleted."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM orchestrator_bottles WHERE bottle_id = ?", (bottle_id,)
            )
            return cur.rowcount > 0

    def get(self, bottle_id: str) -> BottleRecord | None:
        """Return the bottle by id, or None if absent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM orchestrator_bottles WHERE bottle_id = ?", (bottle_id,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def all(self) -> list[BottleRecord]:
        """Every registered bottle, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orchestrator_bottles ORDER BY created_at"
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def by_source_ip(self, source_ip: str) -> BottleRecord | None:
        """Network-layer attribution: the single active bottle at this source
        IP, or None if unknown or ambiguous (more than one — a
        misconfiguration). Safe as the *sole* attributor only where the
        source IP is unspoofable (Firecracker `/31` + nft) and the control
        plane is reachable only by the trusted gateway; pair with the
        identity token (`attribute`) elsewhere."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orchestrator_bottles "
                "WHERE source_ip = ? AND state = 'active'",
                (source_ip,),
            ).fetchall()
        if len(rows) != 1:
            return None
        return _row_to_record(rows[0])

    def attribute(self, source_ip: str, identity_token: str) -> BottleRecord | None:
        """Fail-closed attribution: `by_source_ip` AND a matching identity
        token (constant-time). Either signal alone is insufficient here — an
        unknown/ambiguous IP, an empty token, or a token mismatch all deny."""
        if not identity_token:
            return None
        rec = self.by_source_ip(source_ip)
        if rec is None:
            return None
        if not hmac.compare_digest(rec.identity_token, identity_token):
            return None
        return rec


__all__ = [
    "BottleRecord",
    "RegistryStore",
    "new_identity_token",
    "default_db_path",
    "IDENTITY_TOKEN_BYTES",
]
