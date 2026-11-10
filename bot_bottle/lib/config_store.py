"""SQLite store for host-side bot-bottle settings."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from .db_store import PORTABLE, DbStore
from .migrations import TableMigrations


DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS = 1
# `audit.delivery` values, for the host default and a bottle's own.
AUDIT_DELIVERIES = ("best_effort", "required")
# The default for bottles that don't set `audit.delivery`.
DEFAULT_AUDIT_DELIVERY = "best_effort"
DEFAULT_TRANSCRIPT_RETENTION_DAYS = 30


@dataclass(frozen=True)
class HostBudget:
    """What bottles may use on this host, in whole GiB (`bot-bottle host
    budget`): memory allocated to running bottles, and storage in bot-bottle's
    own directories. A launch that wouldn't fit is refused. 0 = not set."""

    disk_gb: int = 0
    memory_gb: int = 0


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


@dataclass(frozen=True)
class GitHubApp:
    """The GitHub App this host's GitHub access goes through (`forge connect
    github --app`). Both are public; "" means bot-bottle's own app."""

    slug: str = ""
    client_id: str = ""


@dataclass(frozen=True)
class HostConfig:
    """Every host setting the CLI shows or sets, as the host controller serves
    them (`HOST_CONFIG_PATH`)."""

    audit_delivery: str = DEFAULT_AUDIT_DELIVERY
    audit_forward: AuditForward | None = None
    firehose: FirehoseSettings = FirehoseSettings()
    transcript_retention_days: int = DEFAULT_TRANSCRIPT_RETENTION_DAYS
    host_budget: HostBudget = HostBudget()
    github_app: GitHubApp = GitHubApp()

    def to_json(self) -> dict[str, object]:
        forward = self.audit_forward
        return {
            "audit_delivery": self.audit_delivery,
            "audit_forward": None if forward is None else
            {"url": forward.url, "secret": forward.secret, "format": forward.format},
            "firehose": {"sinks": [s.to_json() for s in self.firehose.sinks],
                         "request_bodies": self.firehose.request_bodies,
                         "response_bodies": self.firehose.response_bodies,
                         "max_body_bytes": self.firehose.max_body_bytes},
            "transcript_retention_days": self.transcript_retention_days,
            "host_budget": {"disk_gb": self.host_budget.disk_gb,
                            "memory_gb": self.host_budget.memory_gb},
            "github_app": {"slug": self.github_app.slug,
                           "client_id": self.github_app.client_id},
        }

    @classmethod
    def from_json(cls, raw: object) -> "HostConfig":
        """The settings in `raw`, the whole of `to_json()`'s shape. ValueError
        if it isn't that shape."""
        body = _object(raw, "config")
        config = cls()
        for name in body:
            config = config.with_json(name, body[name])
        return config

    def with_json(self, name: str, value: object) -> "HostConfig":
        """This config with setting `name` set from its JSON `value`.
        ValueError for an unknown setting or a bad value."""
        if name == "audit_delivery":
            if value not in AUDIT_DELIVERIES:
                raise ValueError(f"audit_delivery must be one of {list(AUDIT_DELIVERIES)}")
            return replace(self, audit_delivery=str(value))
        if name == "audit_forward":
            return replace(self, audit_forward=None if value is None else _forward(value))
        if name == "firehose":
            return replace(self, firehose=_firehose(value))
        if name == "transcript_retention_days":
            return replace(self, transcript_retention_days=_int(value, name, minimum=1))
        if name == "host_budget":
            body = _object(value, name)
            return replace(self, host_budget=HostBudget(
                disk_gb=_int(body.get("disk_gb", 0), "disk_gb", minimum=0),
                memory_gb=_int(body.get("memory_gb", 0), "memory_gb", minimum=0)))
        if name == "github_app":
            body = _object(value, name)
            slug, client_id = body.get("slug", ""), body.get("client_id", "")
            if not isinstance(slug, str) or not isinstance(client_id, str):
                raise ValueError("github_app's slug and client_id are text")
            return replace(self, github_app=GitHubApp(slug=slug, client_id=client_id))
        raise ValueError(f"unknown host setting {name!r}")


def _object(raw: object, where: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be an object")
    return {str(k): v for k, v in raw.items()}  # type: ignore[misc]


def _int(raw: object, where: str, *, minimum: int) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < minimum:
        raise ValueError(f"{where} must be a whole number of at least {minimum}")
    return raw


def _forward(raw: object) -> AuditForward:
    body = _object(raw, "audit_forward")
    url, secret, fmt = body.get("url"), body.get("secret", ""), body.get("format", "native")
    if not isinstance(url, str) or not url:
        raise ValueError("audit_forward.url must be a URL")
    if not isinstance(secret, str) or fmt not in ("native", "ocsf"):
        raise ValueError("audit_forward needs a secret name and a format: native or ocsf")
    return AuditForward(url=url, secret=secret, format=str(fmt))


def _firehose(raw: object) -> FirehoseSettings:
    body = _object(raw, "firehose")
    sinks = body.get("sinks", [])
    if not isinstance(sinks, list):
        raise ValueError("firehose.sinks must be a list")
    parsed = [FirehoseSink.from_json(s) for s in sinks]  # type: ignore[misc]
    if any(s is None for s in parsed):
        raise ValueError("each firehose sink is {type: exec, command} or {type: https, url}")
    flags = [body.get(k, d) for k, d in (("request_bodies", True), ("response_bodies", False))]
    if not all(isinstance(f, bool) for f in flags):
        raise ValueError("firehose request_bodies and response_bodies must be true or false")
    return FirehoseSettings(
        sinks=tuple(s for s in parsed if s is not None), request_bodies=bool(flags[0]),
        response_bodies=bool(flags[1]),
        max_body_bytes=_int(body.get("max_body_bytes", 65536), "max_body_bytes", minimum=0))


class ConfigStore(DbStore):
    """SQLite store for host-side settings."""

    # Host settings travel with the host's owner.
    TABLES = {"bot_bottle_config": PORTABLE}

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
            # v10: how long transcript copies are kept
            f"""
            ALTER TABLE bot_bottle_config
            ADD COLUMN transcript_retention_days INTEGER NOT NULL
                DEFAULT {DEFAULT_TRANSCRIPT_RETENTION_DAYS}
            """,
            # v11: the host budget, `bot-bottle host budget` (GiB; 0 = none)
            """
            ALTER TABLE bot_bottle_config
            ADD COLUMN host_max_disk_gb INTEGER NOT NULL DEFAULT 0
            """,
            """
            ALTER TABLE bot_bottle_config
            ADD COLUMN host_max_memory_gb INTEGER NOT NULL DEFAULT 0
            """,
            # v13-14: the GitHub App's public slug and client id ("" = bot-bottle's)
            """
            ALTER TABLE bot_bottle_config
            ADD COLUMN github_app_slug TEXT NOT NULL DEFAULT ''
            """,
            """
            ALTER TABLE bot_bottle_config
            ADD COLUMN github_app_client_id TEXT NOT NULL DEFAULT ''
            """,
        ])
        super().__init__(db_path, migrations)

    def host_config(self) -> HostConfig:
        """Every setting `HostConfig` carries."""
        return HostConfig(audit_delivery=self.audit_delivery(), audit_forward=self.audit_forward(),
                          firehose=self.firehose(),
                          transcript_retention_days=self.transcript_retention_days(),
                          host_budget=self.host_budget(), github_app=self.github_app())

    def set_host_config(self, config: HostConfig) -> None:
        """Store each of `config`'s settings."""
        self.migrate()
        self.set_audit_delivery(config.audit_delivery)
        self.set_audit_forward(config.audit_forward)
        self.set_firehose(config.firehose)
        self.set_transcript_retention_days(config.transcript_retention_days)
        self.set_host_budget(config.host_budget)
        self.set_github_app(config.github_app)

    def cached_image_stale_warning_days(self) -> int:
        if not self.db_path.is_file():
            return DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS
        with self._connection() as conn:
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
            with self._connection() as conn:
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
            with self._connection() as conn:
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
        with self._connection() as conn:
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
            with self._connection() as conn:
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
        with self._connection() as conn:
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

    def transcript_retention_days(self) -> int:
        """Days a closed session's transcript copies are kept (PRD 0095)."""
        if not self.db_path.is_file():
            return DEFAULT_TRANSCRIPT_RETENTION_DAYS
        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT transcript_retention_days FROM bot_bottle_config WHERE id = 1",
                ).fetchone()
        except sqlite3.OperationalError:
            return DEFAULT_TRANSCRIPT_RETENTION_DAYS  # not migrated yet
        return int(row["transcript_retention_days"]) if row is not None \
            else DEFAULT_TRANSCRIPT_RETENTION_DAYS

    def set_transcript_retention_days(self, days: int) -> Path:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO bot_bottle_config (id, transcript_retention_days)
                VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE
                SET transcript_retention_days = excluded.transcript_retention_days
                """,
                (days,),
            )
        self._chmod()
        return self.db_path

    def host_budget(self) -> HostBudget:
        """The bottles' memory and storage budget (`bot-bottle host budget`);
        0 = none set."""
        if not self.db_path.is_file():
            return HostBudget()
        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT host_max_disk_gb, host_max_memory_gb FROM bot_bottle_config "
                    "WHERE id = 1",
                ).fetchone()
        except sqlite3.OperationalError:
            return HostBudget()  # not migrated yet
        if row is None:
            return HostBudget()
        return HostBudget(disk_gb=int(row["host_max_disk_gb"]),
                          memory_gb=int(row["host_max_memory_gb"]))

    def set_host_budget(self, caps: HostBudget) -> Path:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO bot_bottle_config (id, host_max_disk_gb, host_max_memory_gb)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    host_max_disk_gb = excluded.host_max_disk_gb,
                    host_max_memory_gb = excluded.host_max_memory_gb
                """,
                (caps.disk_gb, caps.memory_gb),
            )
        self._chmod()
        return self.db_path

    def github_app(self) -> GitHubApp:
        if not self.db_path.is_file():
            return GitHubApp()
        try:
            with self._connection() as conn:
                row = conn.execute("SELECT github_app_slug, github_app_client_id "
                                   "FROM bot_bottle_config WHERE id = 1").fetchone()
        except sqlite3.OperationalError:
            return GitHubApp()  # not migrated yet
        return GitHubApp(slug=str(row["github_app_slug"]),
                         client_id=str(row["github_app_client_id"])) if row else GitHubApp()

    def set_github_app(self, app: GitHubApp) -> Path:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO bot_bottle_config (id, github_app_slug, github_app_client_id)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    github_app_slug = excluded.github_app_slug,
                    github_app_client_id = excluded.github_app_client_id
                """,
                (app.slug, app.client_id),
            )
        self._chmod()
        return self.db_path

    def set_audit_delivery(self, delivery: str) -> Path:
        with self._connection() as conn:
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
        with self._connection() as conn:
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
    "AUDIT_DELIVERIES",
    "AuditForward",
    "GitHubApp",
    "HostBudget",
    "HostConfig",
    "FirehoseSettings",
    "FirehoseSink",
    "DEFAULT_AUDIT_DELIVERY",
    "DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS",
    "ConfigStore",
]
