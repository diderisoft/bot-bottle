"""Host filesystem paths. `bot_bottle_root()` (default `~/.bot-bottle`, or
`~/.bot-bottle-envs/<name>` in a named environment; overridable with
`BOT_BOTTLE_ROOT`) holds per-bottle state, keys, the gateway
CA and the host DB. Services are handed the paths they need at startup.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

from .contract.auth import ORCHESTRATOR_TOKEN_FILENAME
from .environment import LIVE, default_root

# The host DB: the host's source-of-truth state, written by the host
# controller (ADR 0020). Its stores share it, namespaced by TableMigrations
# schema_key.
HOST_DB_FILENAME = "bot-bottle.db"
# The orchestrator's own DB (registry, egress secrets, supervise queue,
# canary state), wherever its backend keeps it. Never the host DB.
REGISTRY_DB_FILENAME = "registry.db"

# The gateway's persistent mitmproxy CA, mounted at mitmproxy's confdir so it
# survives recreation.
GATEWAY_CA_DIRNAME = "gateway-ca"

# `NAME=value` lines, mode 0600.
SECRETS_FILENAME = "secrets"

# The audit journal's segments. Not under `db/`, which is mounted into infra.
AUDIT_DIRNAME = "audit"

# Audit records sent to the console (ADR 0013): the AES-256-GCM key they're
# encrypted with (hex, 0600; never leaves the host), and how far each agent's
# records have been sent. The passkeys allowed to unlock reading them are in
# the host DB (`audit_trust`).
AUDIT_KEY_FILENAME = "audit.key"
AUDIT_PUSH_DIRNAME = "audit-push"


def bot_bottle_root(home: Path | None = None) -> Path:
    """The app root, data and manifests (agents/, bottles/, contrib/) alike:
    `$BOT_BOTTLE_ROOT` if set, else `<home>/.bot-bottle` for the live
    environment and `<home>/.bot-bottle-envs/<name>` for a named one (`home`
    defaults to the user's)."""
    override = os.environ.get("BOT_BOTTLE_ROOT")
    if override:
        return Path(override)
    return default_root(home=home)


def project_manifest_dir(cwd: str | Path, *, home: Path | None = None,
                         root: Path | None = None) -> Path | None:
    """`<cwd>/.bot-bottle` when it holds a project's manifests, else None.
    A bot-bottle root is never a project: neither the active one (`root`,
    default `bot_bottle_root(home)`) nor the live environment's
    `~/.bot-bottle`, which is what a named environment sees from the home
    directory."""
    project = Path(cwd) / ".bot-bottle"
    if not project.is_dir():
        return None
    home = home if home is not None else Path.home()
    roots = {(root if root is not None else bot_bottle_root(home)).resolve(),
             default_root(LIVE, home).resolve()}
    return None if project.resolve() in roots else project


def host_secrets_path() -> Path:
    """The host secrets file, `<root>/secrets`."""
    return bot_bottle_root() / SECRETS_FILENAME


def host_db_path() -> Path:
    """`<root>/db/bot-bottle.db`, the host DB. Only the host controller opens
    it (ADR 0020); the CLI goes through its API."""
    return bot_bottle_root() / "db" / HOST_DB_FILENAME


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


def audit_key_path() -> Path:
    return bot_bottle_root() / AUDIT_KEY_FILENAME


def audit_key_bytes() -> bytes:
    """The host's 32-byte audit record key, created 0600 on first use (stored
    hex). A file that isn't one is an error, never silently replaced: records
    already sent are only readable with it."""
    path = audit_key_path()
    try:
        text = path.read_text().strip()
    except FileNotFoundError:
        text = ""
    if not text:
        path.parent.mkdir(parents=True, exist_ok=True)
        key = os.urandom(32)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            text = path.read_text().strip()
        else:
            with os.fdopen(fd, "w") as f:
                f.write(key.hex())
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            return key
    try:
        key = bytes.fromhex(text)
    except ValueError as e:
        raise ValueError(f"{path} isn't an audit key") from e
    if len(key) != 32:
        raise ValueError(f"{path} isn't an audit key")
    return key


def audit_push_dir() -> Path:
    return bot_bottle_root() / AUDIT_PUSH_DIRNAME


def host_orchestrator_token() -> str:
    """The `control-plane` domain's signing key."""
    return host_signing_key(ORCHESTRATOR_TOKEN_FILENAME)


def host_artifact_cache_dir(override: str = "") -> Path:
    """Verified artifact downloads: `override` (`InfraSettings.artifact_cache`)
    if set, else under `~/.cache`, since everything here is re-downloadable."""
    if override:
        return Path(override)
    return Path.home() / ".cache" / "bot-bottle" / "artifacts"


def host_firecracker_cache_path() -> Path:
    """Where the firecracker cache root is, without creating it.
    `BOT_BOTTLE_FC_CACHE` relocates it."""
    return Path(os.environ.get(
        "BOT_BOTTLE_FC_CACHE", str(Path.home() / ".cache" / "bot-bottle" / "firecracker"),
    ))


def host_firecracker_cache_dir() -> Path:
    """The firecracker cache root (kernel, rootfs, run dirs), created on
    demand."""
    d = host_firecracker_cache_path()
    d.mkdir(parents=True, exist_ok=True)
    return d


__all__ = [
    "AUDIT_DIRNAME",
    "AUDIT_KEY_FILENAME",
    "AUDIT_PUSH_DIRNAME",
    "audit_key_bytes",
    "audit_key_path",
    "audit_push_dir",
    "HOST_DB_FILENAME",
    "REGISTRY_DB_FILENAME",
    "host_audit_dir",
    "host_artifact_cache_dir",
    "host_firecracker_cache_dir",
    "GATEWAY_CA_DIRNAME",
    "bot_bottle_root",
    "host_db_path",
    "host_gateway_ca_dir",
    "host_signing_key",
    "host_orchestrator_token",
    "host_secrets_path",
    "SECRETS_FILENAME",
]
