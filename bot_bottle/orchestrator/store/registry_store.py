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
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ...store.db_store import DbStore
from ...store.migrations import TableMigrations
from ...paths import host_db_path

# 256 bits of urandom, URL-safe — unguessable per-bottle identity token.
IDENTITY_TOKEN_BYTES = 32

# How recently a row must have been registered to be exempt from
# `reap_absent`. Covers the window between `container run` and the address
# becoming visible to another launch's enumeration, so reconciliation never
# reaps a bottle that is still coming up.
DEFAULT_REAP_GRACE_SECONDS = 120.0


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
        # v4 — per-bottle encrypted egress secrets (PRD 0080).
        # One row per env-var: key (env-var name) is plaintext for auditing;
        # value is the encrypted token string. The encryption key (ENV_VAR_SECRET)
        # lives only in the agent's environment — a row alone cannot recover the
        # credential.
        """
        CREATE TABLE IF NOT EXISTS bottled_agent_secrets (
            bottled_agent_id TEXT NOT NULL,
            key              TEXT NOT NULL,
            value            TEXT NOT NULL,
            type             TEXT NOT NULL DEFAULT 'injected_env_var'
        )
        """,
        # v5 — index for fast per-bottle lookups and bulk DELETE on teardown.
        "CREATE INDEX IF NOT EXISTS idx_bottled_agent_secrets_id "
        "ON bottled_agent_secrets (bottled_agent_id, type)",
        # v6 — unauthenticated legacy ciphertext must never be selected by
        # attacker-controlled blob contents. Existing local agents are
        # intentionally reprovisioned instead of retaining downgrade support.
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
        when not supplied. Live — this is the control-plane reload path.

        Supersedes any *other* active bottle at the same source IP first:
        `by_source_ip` fail-closes on ambiguity (>1 active row → None), so a
        leftover row at a reused IP — from an untorn-down bottle, crash
        recovery, or a persisted DB — would otherwise *brick* the new bottle's
        attribution. The new registration is authoritative for its IP; the
        stale rows go. INSERT OR REPLACE handles a same-`bottle_id` reload in
        place (its own row is exempt from the sweep)."""
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
        """Update a bottle's policy in place (live reload). Returns True if
        the bottle exists."""
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
        """Delete active rows whose source IP is not held by a live bottle.

        A row only ever leaves the registry two ways: an explicit
        `teardown_bottle` (the launcher's cleanup callback) or the supersede
        sweep in `register`. Neither runs when the launching CLI dies hard —
        SIGKILL, a closed terminal, a host sleep/crash — so the row outlives
        its container. That orphan is not inert: source IPs are recycled by
        the backend's DHCP, and `by_source_ip` fail-closes on ambiguity, so a
        leftover row at a reused address can brick the *next* bottle that
        lands on it (no policy resolved -> every host denied, reported to the
        agent as "not in the allowlist"). Reconciling against the live set at
        launch keeps the registry from accumulating those landmines.

        Restores the invariant the data plane needs: **at most one active row
        per live address, and none at all for a dead one.** Two cases, because
        a dead bottle's address may already have been handed to a live one:

          * no live bottle holds the address — every row there is an orphan;
          * a live bottle holds it but several rows claim it — the newest
            registration is authoritative and the rest are orphans, the same
            rule `register`'s same-IP supersede sweep applies. Without this
            second case a recycled address stays ambiguous, which is exactly
            the state that resolves no policy.

        `grace_seconds` protects an in-flight launch: registration happens
        moments after `container run`, and a concurrent launch's address may
        not be visible to the caller's enumeration yet. Rows younger than the
        grace window are never reaped, so reconciliation can't race a bottle
        that is still coming up. Returns the deleted records."""
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
                # Keep the newest claim on a live address; supersede the rest.
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
        """Network-layer attribution: the single active bottle at this source
        IP, or None if unknown or ambiguous (more than one — a
        misconfiguration). Safe as the *sole* attributor only where the
        source IP is unspoofable (Firecracker `/31` + nft) and the control
        plane is reachable only by the trusted gateway; pair with the
        identity token (`attribute`) elsewhere."""
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

    # --- encrypted egress secret store ------------------------------------

    def store_agent_secrets(
        self,
        bottle_id: str,
        encrypted_values: dict[str, str],
        secret_type: str = "injected_env_var",
    ) -> None:
        """Replace all stored secrets for *bottle_id* with *encrypted_values*
        (env-var name → encrypted ciphertext). Deletes then re-inserts so a
        re-registration is always consistent with the current token set."""
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
        """Upsert ONE encrypted secret row (env-var name → ciphertext) for
        *bottle_id*, leaving the bottle's other secrets untouched — the per-key
        counterpart of ``store_agent_secrets``' replace-all. Delete-then-insert
        because the table carries no unique constraint to `ON CONFLICT` against."""
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
        """Return {env_var_name: encrypted_value} for *bottle_id*, or {} if none."""
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
        """Remove all stored secrets for *bottle_id* (e.g. on teardown)."""
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
    "default_db_path",
    "IDENTITY_TOKEN_BYTES",
    "DEFAULT_REAP_GRACE_SECONDS",
]
