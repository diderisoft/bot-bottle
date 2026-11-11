"""SQLite store for host-side bot-bottle settings."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from .db_store import MACHINE_SPECIFIC, MIXED, PORTABLE, DbStore
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
class AuditRetention:
    """How much of the audit journal is kept (`bot-bottle audit retention`):
    whole segments older than `days`, or past `max_gb` GiB in all, are pruned.
    0 = no limit; both 0 (the default) keeps everything."""

    days: int = 0
    max_gb: int = 0

    @property
    def keeps_all(self) -> bool:
        return not self.days and not self.max_gb


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
    audit_retention: AuditRetention = AuditRetention()
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
            "audit_retention": {"days": self.audit_retention.days,
                                "max_gb": self.audit_retention.max_gb},
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

    def with_json(  # pylint: disable=too-many-return-statements  # one per setting
            self, name: str, value: object) -> "HostConfig":
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
        if name == "audit_retention":
            body = _object(value, name)
            return replace(self, audit_retention=AuditRetention(
                days=_int(body.get("days", 0), "days", minimum=0),
                max_gb=_int(body.get("max_gb", 0), "max_gb", minimum=0)))
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


# The settings `HostConfig` carries, as `HOST_CONFIG_PATH` names them.
HOST_SETTINGS = ("audit_delivery", "audit_forward", "firehose", "transcript_retention_days",
                 "audit_retention", "host_budget", "github_app")


def _columns(  # pylint: disable=too-many-return-statements  # one per setting
        config: HostConfig, name: str) -> dict[str, object]:
    """The `bot_bottle_config` columns that hold setting `name`, with
    `config`'s values. ValueError for an unknown setting."""
    if name == "audit_delivery":
        return {"audit_delivery": config.audit_delivery}
    if name == "audit_forward":
        forward = config.audit_forward or AuditForward(url="", secret="", format="native")
        return {"audit_forward_url": forward.url, "audit_forward_secret": forward.secret,
                "audit_forward_format": forward.format}
    if name == "firehose":
        hose = config.firehose
        return {"firehose_sinks": json.dumps([s.to_json() for s in hose.sinks]),
                "firehose_request_bodies": int(hose.request_bodies),
                "firehose_response_bodies": int(hose.response_bodies),
                "firehose_max_body_bytes": hose.max_body_bytes}
    if name == "transcript_retention_days":
        return {"transcript_retention_days": config.transcript_retention_days}
    if name == "audit_retention":
        return {"audit_retention_days": config.audit_retention.days,
                "audit_retention_max_gb": config.audit_retention.max_gb}
    if name == "host_budget":
        return {"host_max_disk_gb": config.host_budget.disk_gb,
                "host_max_memory_gb": config.host_budget.memory_gb}
    if name == "github_app":
        return {"github_app_slug": config.github_app.slug,
                "github_app_client_id": config.github_app.client_id}
    raise ValueError(f"unknown host setting {name!r}")


class ConfigStore(DbStore):
    """SQLite store for host-side settings."""

    # Most host settings travel with the host's owner. What's sized to this
    # machine (its budget, the journal's size cap, how stale its image cache
    # may get) or runs its programs (exec firehose sinks) doesn't.
    TABLES = {"bot_bottle_config": MIXED}
    COLUMNS = {"bot_bottle_config": {
        "id": PORTABLE,
        "cached_image_stale_warning_days": MACHINE_SPECIFIC,
        "audit_delivery": PORTABLE,
        "audit_forward_url": PORTABLE, "audit_forward_secret": PORTABLE,
        "audit_forward_format": PORTABLE,
        "firehose_sinks": MACHINE_SPECIFIC,
        "firehose_request_bodies": PORTABLE, "firehose_response_bodies": PORTABLE,
        "firehose_max_body_bytes": PORTABLE,
        "transcript_retention_days": PORTABLE,
        "host_max_disk_gb": MACHINE_SPECIFIC, "host_max_memory_gb": MACHINE_SPECIFIC,
        "github_app_slug": PORTABLE, "github_app_client_id": PORTABLE,
        "audit_retention_days": PORTABLE, "audit_retention_max_gb": MACHINE_SPECIFIC,
    }}

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
            # v15-16: audit journal retention, `bot-bottle audit retention` (0 = none)
            """
            ALTER TABLE bot_bottle_config
            ADD COLUMN audit_retention_days INTEGER NOT NULL DEFAULT 0
            """,
            """
            ALTER TABLE bot_bottle_config
            ADD COLUMN audit_retention_max_gb INTEGER NOT NULL DEFAULT 0
            """,
        ])
        super().__init__(db_path, migrations)

    def host_config(self) -> HostConfig:
        """Every setting `HostConfig` carries."""
        return HostConfig(audit_delivery=self.audit_delivery(), audit_forward=self.audit_forward(),
                          firehose=self.firehose(),
                          transcript_retention_days=self.transcript_retention_days(),
                          audit_retention=self.audit_retention(),
                          host_budget=self.host_budget(), github_app=self.github_app())

    def set_host_config(self, config: HostConfig, names: Iterable[str] | None = None) -> None:
        """Store the settings `names` (default: every one) as `config` has
        them, in one transaction. Settings it doesn't name are left as they
        are, so concurrent changes to different settings both land."""
        columns: dict[str, object] = {}
        for name in HOST_SETTINGS if names is None else names:
            columns.update(_columns(config, name))
        self.migrate()
        self._set_columns(columns)

    def _set_columns(self, columns: dict[str, object]) -> Path:
        """Upsert `columns` into the one settings row."""
        if columns:
            names = list(columns)
            with self._connection() as conn:
                conn.execute(
                    f"INSERT INTO bot_bottle_config (id, {', '.join(names)}) "
                    f"VALUES (1, {', '.join('?' for _ in names)}) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    + ", ".join(f"{n} = excluded.{n}" for n in names),
                    tuple(columns.values()),
                )
            self._chmod()
        return self.db_path

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
        return self._set_columns(_columns(HostConfig(audit_forward=forward), "audit_forward"))

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
        return self._set_columns(_columns(HostConfig(firehose=settings), "firehose"))

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
        return self._set_columns(
            _columns(HostConfig(transcript_retention_days=days), "transcript_retention_days"))

    def audit_retention(self) -> AuditRetention:
        """How much of the audit journal is kept (`bot-bottle audit
        retention`); keep-all unless set."""
        if not self.db_path.is_file():
            return AuditRetention()
        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT audit_retention_days, audit_retention_max_gb "
                    "FROM bot_bottle_config WHERE id = 1",
                ).fetchone()
        except sqlite3.OperationalError:
            return AuditRetention()  # not migrated yet
        if row is None:
            return AuditRetention()
        return AuditRetention(days=int(row["audit_retention_days"]),
                              max_gb=int(row["audit_retention_max_gb"]))

    def set_audit_retention(self, retention: AuditRetention) -> Path:
        return self._set_columns(
            _columns(HostConfig(audit_retention=retention), "audit_retention"))

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
        return self._set_columns(_columns(HostConfig(host_budget=caps), "host_budget"))

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
        return self._set_columns(_columns(HostConfig(github_app=app), "github_app"))

    def set_audit_delivery(self, delivery: str) -> Path:
        return self._set_columns(
            _columns(HostConfig(audit_delivery=delivery), "audit_delivery"))

    def set_cached_image_stale_warning_days(self, days: int) -> Path:
        return self._set_columns({"cached_image_stale_warning_days": days})


__all__ = [
    "AUDIT_DELIVERIES",
    "AuditForward",
    "AuditRetention",
    "GitHubApp",
    "HostBudget",
    "HostConfig",
    "FirehoseSettings",
    "FirehoseSink",
    "DEFAULT_AUDIT_DELIVERY",
    "DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS",
    "ConfigStore",
    "HOST_SETTINGS",
]
