"""SQLite store for host-side bot-bottle settings."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .db_store import DbStore
from .migrations import TableMigrations


DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS = 1
# The default for bottles that don't set `audit.delivery`.
DEFAULT_AUDIT_DELIVERY = "best_effort"


@dataclass(frozen=True)
class AuditForward:
    """An HTTPS endpoint the journal is forwarded to. `secret` names a
    secrets-file entry sent as a bearer token."""

    url: str
    secret: str
    format: str  # native | ocsf


@dataclass(frozen=True)
class FirehoseSink:
    """A firehose sink: `exec` writes JSONL to `command`'s stdin; `https` POSTs
    NDJSON batches to `url` with the secrets-file entry `secret` as a bearer
    token."""

    type: str  # exec | https
    command: tuple[str, ...] = ()
    url: str = ""
    secret: str = ""

    def to_json(self) -> dict[str, object]:
        if self.type == "exec":
            return {"type": "exec", "command": list(self.command)}
        return {"type": "https", "url": self.url, "secret": self.secret}

    @classmethod
    def from_json(cls, raw: object) -> "FirehoseSink | None":
        if not isinstance(raw, dict):
            return None
        if raw.get("type") == "exec" and isinstance(raw.get("command"), list) and raw["command"]:
            return cls("exec", command=tuple(str(c) for c in raw["command"]))
        if raw.get("type") == "https" and isinstance(raw.get("url"), str) and raw["url"]:
            return cls("https", url=raw["url"], secret=str(raw.get("secret") or ""))
        return None


@dataclass(frozen=True)
class FirehoseSettings:
    sinks: tuple[FirehoseSink, ...] = ()
    request_bodies: bool = True
    response_bodies: bool = False
    max_body_bytes: int = 65536


class ConfigStore(DbStore):
    """SQLite store for host-side settings."""

    def __init__(self, db_path: Path) -> None:
        migrations = TableMigrations("config_store", [
            # v1: host settings
            """
            CREATE TABLE IF NOT EXISTS bot_bottle_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cached_image_stale_warning_days INTEGER NOT NULL DEFAULT 1
            )
            """,
            # v2: host-wide audit delivery default
            """
            ALTER TABLE bot_bottle_config
            ADD COLUMN audit_delivery TEXT NOT NULL DEFAULT 'best_effort'
            """,
            # v3-5: audit journal forwarding
            """
            ALTER TABLE bot_bottle_config
            ADD COLUMN audit_forward_url TEXT NOT NULL DEFAULT ''
            """,
            """
            ALTER TABLE bot_bottle_config
            ADD COLUMN audit_forward_secret TEXT NOT NULL DEFAULT ''
            """,
            """
            ALTER TABLE bot_bottle_config
            ADD COLUMN audit_forward_format TEXT NOT NULL DEFAULT 'native'
            """,
            # v6-9: firehose sinks and capture
            """
            ALTER TABLE bot_bottle_config
            ADD COLUMN firehose_sinks TEXT NOT NULL DEFAULT '[]'
            """,
            """
            ALTER TABLE bot_bottle_config
            ADD COLUMN firehose_request_bodies INTEGER NOT NULL DEFAULT 1
            """,
            """
            ALTER TABLE bot_bottle_config
            ADD COLUMN firehose_response_bodies INTEGER NOT NULL DEFAULT 0
            """,
            """
            ALTER TABLE bot_bottle_config
            ADD COLUMN firehose_max_body_bytes INTEGER NOT NULL DEFAULT 65536
            """,
        ])
        super().__init__(db_path, migrations)

    def cached_image_stale_warning_days(self) -> int:
        if not self.db_path.is_file():
            return DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT cached_image_stale_warning_days
                FROM bot_bottle_config
                WHERE id = 1
                """,
            ).fetchone()
        if row is None:
            return DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS
        try:
            return int(row["cached_image_stale_warning_days"])
        except (TypeError, ValueError):
            return DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS

    def audit_delivery(self) -> str:
        """The host-wide `audit.delivery` default."""
        if not self.db_path.is_file():
            return DEFAULT_AUDIT_DELIVERY
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT audit_delivery FROM bot_bottle_config WHERE id = 1",
                ).fetchone()
        except sqlite3.OperationalError:
            return DEFAULT_AUDIT_DELIVERY  # not migrated yet
        return str(row["audit_delivery"]) if row is not None else DEFAULT_AUDIT_DELIVERY

    def audit_forward(self) -> AuditForward | None:
        """Where to forward the audit journal, or None when it isn't."""
        if not self.db_path.is_file():
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT audit_forward_url, audit_forward_secret, audit_forward_format "
                    "FROM bot_bottle_config WHERE id = 1",
                ).fetchone()
        except sqlite3.OperationalError:
            return None  # not migrated yet
        if row is None or not row["audit_forward_url"]:
            return None
        return AuditForward(url=str(row["audit_forward_url"]),
                            secret=str(row["audit_forward_secret"]),
                            format=str(row["audit_forward_format"]))

    def set_audit_forward(self, forward: AuditForward | None) -> Path:
        """Forward the journal as `forward` says; None stops forwarding."""
        forward = forward or AuditForward(url="", secret="", format="native")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_bottle_config
                    (id, audit_forward_url, audit_forward_secret, audit_forward_format)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    audit_forward_url = excluded.audit_forward_url,
                    audit_forward_secret = excluded.audit_forward_secret,
                    audit_forward_format = excluded.audit_forward_format
                """,
                (forward.url, forward.secret, forward.format),
            )
        self._chmod()
        return self.db_path

    def firehose(self) -> FirehoseSettings:
        """The request firehose's sinks and capture settings."""
        if not self.db_path.is_file():
            return FirehoseSettings()
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT firehose_sinks, firehose_request_bodies, firehose_response_bodies, "
                    "firehose_max_body_bytes FROM bot_bottle_config WHERE id = 1",
                ).fetchone()
        except sqlite3.OperationalError:
            return FirehoseSettings()  # not migrated yet
        if row is None:
            return FirehoseSettings()
        try:
            raw = json.loads(row["firehose_sinks"] or "[]")
        except ValueError:
            raw = []
        sinks = tuple(s for s in (FirehoseSink.from_json(r) for r in raw) if s is not None) \
            if isinstance(raw, list) else ()
        return FirehoseSettings(sinks=sinks, request_bodies=bool(row["firehose_request_bodies"]),
                                response_bodies=bool(row["firehose_response_bodies"]),
                                max_body_bytes=int(row["firehose_max_body_bytes"]))

    def set_firehose(self, settings: FirehoseSettings) -> Path:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_bottle_config (id, firehose_sinks, firehose_request_bodies,
                    firehose_response_bodies, firehose_max_body_bytes)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    firehose_sinks = excluded.firehose_sinks,
                    firehose_request_bodies = excluded.firehose_request_bodies,
                    firehose_response_bodies = excluded.firehose_response_bodies,
                    firehose_max_body_bytes = excluded.firehose_max_body_bytes
                """,
                (json.dumps([s.to_json() for s in settings.sinks]), int(settings.request_bodies),
                 int(settings.response_bodies), settings.max_body_bytes),
            )
        self._chmod()
        return self.db_path

    def set_audit_delivery(self, delivery: str) -> Path:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_bottle_config (id, audit_delivery)
                VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET audit_delivery = excluded.audit_delivery
                """,
                (delivery,),
            )
        self._chmod()
        return self.db_path

    def set_cached_image_stale_warning_days(self, days: int) -> Path:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_bottle_config (id, cached_image_stale_warning_days)
                VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET
                    cached_image_stale_warning_days = excluded.cached_image_stale_warning_days
                """,
                (days,),
            )
        self._chmod()
        return self.db_path


__all__ = [
    "AuditForward",
    "FirehoseSettings",
    "FirehoseSink",
    "DEFAULT_AUDIT_DELIVERY",
    "DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS",
    "ConfigStore",
]
