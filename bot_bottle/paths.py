"""Host filesystem paths. `bot_bottle_root()` (default `~/.bot-bottle`,
overridable with `BOT_BOTTLE_ROOT`) holds per-bottle state, keys, the gateway
CA and the host DB. Services are handed the paths they need at startup.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

from .contract.auth import ORCHESTRATOR_TOKEN_FILENAME

# The host DB; every SQLite store shares it, namespaced by TableMigrations
# schema_key.
HOST_DB_FILENAME = "bot-bottle.db"

# The gateway's persistent mitmproxy CA, mounted at mitmproxy's confdir so it
# survives recreation.
GATEWAY_CA_DIRNAME = "gateway-ca"

# `NAME=value` lines, mode 0600.
SECRETS_FILENAME = "secrets"

# The audit journal's segments. Not under `db/`, which is mounted into infra.
AUDIT_DIRNAME = "audit"


def bot_bottle_root() -> Path:
    """The app data root — `$BOT_BOTTLE_ROOT` if set, else `~/.bot-bottle`."""
    override = os.environ.get("BOT_BOTTLE_ROOT")
    return Path(override) if override else Path.home() / ".bot-bottle"


def host_secrets_path() -> Path:
    """The host secrets file, `<root>/secrets`."""
    return bot_bottle_root() / SECRETS_FILENAME


def host_db_path() -> Path:
    """`<root>/db/bot-bottle.db`. In its own directory so a backend that mounts
    only directories can share it without exposing the rest of the root."""
    return bot_bottle_root() / "db" / HOST_DB_FILENAME


def host_db_dir() -> Path:
    """The host DB's directory, created if missing."""
    db_dir = host_db_path().parent
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir


def host_audit_dir() -> Path:
    """`<root>/audit/`. Only the host controller writes here."""
    return bot_bottle_root() / AUDIT_DIRNAME


def host_gateway_ca_dir() -> Path:
    """The gateway CA directory, created if missing. A host directory rather
    than a Docker named volume, which `docker system prune --volumes` would
    wipe and force a CA every running bottle distrusts."""
    ca_dir = bot_bottle_root() / GATEWAY_CA_DIRNAME
    ca_dir.mkdir(parents=True, exist_ok=True)
    return ca_dir


def host_signing_key(filename: str) -> str:
    """A per-host signing key at `<root>/<filename>`: 256-bit, url-safe,
    created 0600 on first use. Each trust domain names its own file, so
    services get distinct keys. The agent never mounts the root."""
    path = bot_bottle_root() / filename
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    # O_EXCL creates it 0600 up front; a lost race re-reads the winner's key.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    with os.fdopen(fd, "w") as f:
        f.write(token)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return token


def host_orchestrator_token() -> str:
    """The `control-plane` domain's signing key."""
    return host_signing_key(ORCHESTRATOR_TOKEN_FILENAME)


def host_artifact_cache_dir(override: str = "") -> Path:
    """Verified artifact downloads: `override` (`InfraSettings.artifact_cache`)
    if set, else under `~/.cache`, since everything here is re-downloadable."""
    if override:
        return Path(override)
    return Path.home() / ".cache" / "bot-bottle" / "artifacts"


def host_firecracker_cache_dir() -> Path:
    """The firecracker cache root (kernel, rootfs, run dirs), created on
    demand. `BOT_BOTTLE_FC_CACHE` relocates it."""
    d = Path(os.environ.get(
        "BOT_BOTTLE_FC_CACHE", str(Path.home() / ".cache" / "bot-bottle" / "firecracker"),
    ))
    d.mkdir(parents=True, exist_ok=True)
    return d


__all__ = [
    "AUDIT_DIRNAME",
    "HOST_DB_FILENAME",
    "host_audit_dir",
    "host_artifact_cache_dir",
    "host_firecracker_cache_dir",
    "GATEWAY_CA_DIRNAME",
    "bot_bottle_root",
    "host_db_path",
    "host_db_dir",
    "host_gateway_ca_dir",
    "host_signing_key",
    "host_orchestrator_token",
    "host_secrets_path",
    "SECRETS_FILENAME",
]
