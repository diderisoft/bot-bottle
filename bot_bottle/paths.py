"""Foundational filesystem paths for bot-bottle.

`bot_bottle_root()` is the app data root — per-bottle state, git-gate
keys, the gateway CA, and the shared SQLite DB all live under it. It
defaults to `~/.bot-bottle` and is overridable with the
**`BOT_BOTTLE_ROOT`** env var.

Note that the supervise queue and the audit log are *tables in the shared
DB*, not directories under the root — see `queue_store.py` / `audit_store.py`.
The root held a `queue/` directory before the SQLite migration;
nothing writes there now.

The env override is the single knob for redirecting the root: the test
suite points it at a throwaway dir instead of monkey-patching the function
(every module and every flat/package copy reads the same env var, so one
override covers them all), and operators can relocate the root if needed.

Host-side only: the services are handed the paths they need at startup.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

from .contract.auth import ORCHESTRATOR_TOKEN_FILENAME

# The single shared host state DB. All bot-bottle SQLite stores (supervise
# queue, audit, the orchestrator registry) co-tenant this one file — the
# TableMigrations schema_key namespaces each store's tables.
HOST_DB_FILENAME = "bot-bottle.db"

# The host directory holding the gateway's persistent mitmproxy CA. Bind-mounted
# into the infra/gateway container at mitmproxy's confdir so the self-generated
# CA survives container recreation — every agent installs this one CA to trust
# the shared gateway's TLS interception, so it must not rotate on restart. See
# host_gateway_ca_dir() for why this is a host bind-mount, not a named volume.
GATEWAY_CA_DIRNAME = "gateway-ca"

# The host secrets file (`NAME=value` lines, mode 0600) under the root.
SECRETS_FILENAME = "secrets"


def bot_bottle_root() -> Path:
    """The app data root — `$BOT_BOTTLE_ROOT` if set, else `~/.bot-bottle`."""
    override = os.environ.get("BOT_BOTTLE_ROOT")
    return Path(override) if override else Path.home() / ".bot-bottle"


def host_secrets_path() -> Path:
    """The host secrets file, `<root>/secrets`."""
    return bot_bottle_root() / SECRETS_FILENAME


def host_db_path() -> Path:
    """Path to the shared host state DB, `<root>/db/bot-bottle.db`.

    Kept in its own `db/` subdirectory (not directly under the root) so a
    backend that can only bind-mount *directories* can share this one file
    with a gateway without exposing the root's other contents (git-gate
    keys, per-bottle state, ...)."""
    return bot_bottle_root() / "db" / HOST_DB_FILENAME


def host_db_dir() -> Path:
    """The directory holding the shared host state DB, created if missing.
    Backends bind-mount this into their gateway so the supervise daemon writes
    to the one DB the orchestrator (and the operator over HTTP) reads."""
    db_dir = host_db_path().parent
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir


def host_gateway_ca_dir() -> Path:
    """The directory holding the gateway's persistent mitmproxy CA, created if
    missing. Backends bind-mount this into the infra/gateway container at
    mitmproxy's confdir so the CA persists across container recreation.

    A host bind-mount under the app-data root — deliberately NOT a Docker
    named volume. A named volume survives `docker rm` but is silently wiped by
    `docker volume prune` / `docker system prune --volumes` during routine host
    maintenance; the gateway then mints a fresh CA that every already-running
    bottle distrusts, failing the TLS handshake even after it reconnects to the
    moved gateway. A path under the root docker never prunes it,
    and it stays directly inspectable + rotatable from the host."""
    ca_dir = bot_bottle_root() / GATEWAY_CA_DIRNAME
    ca_dir.mkdir(parents=True, exist_ok=True)
    return ca_dir


def host_signing_key(filename: str) -> str:
    """A per-host signing key at `<root>/<filename>`, minted (256-bit, url-safe)
    and persisted 0600 on first use, then reused.

    The generic form of `host_orchestrator_token()`: each service names its own
    key file (`contract.auth.TrustDomain`), so the orchestrator and a separate service like
    the host controller get distinct keys neither can read. It is a *host*
    artifact — the file lives under the root the agent never mounts, and its value
    is injected only into the trusted control-plane process — so reading it here
    is safe on the launch path but the value never reaches a bottle."""
    path = bot_bottle_root() / filename
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    # Create 0600 up front (O_EXCL loses a concurrent race harmlessly — we
    # re-read the winner's token below) so the secret is never briefly world-
    # readable between write and chmod.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    with os.fdopen(fd, "w") as f:
        f.write(token)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return token


def host_orchestrator_token() -> str:
    """The per-host control-plane signing key — the host-canonical key the
    launchers inject into the control-plane process and the host CLI mints its
    own `cli` token from. The `control-plane` trust domain's specialization of
    `host_signing_key()`."""
    return host_signing_key(ORCHESTRATOR_TOKEN_FILENAME)


def host_artifact_cache_dir(override: str = "") -> Path:
    """Where verified downloads of published artifacts are kept: `override`
    (`InfraSettings.artifact_cache`) when set.

    Under `~/.cache`, not the app-data root: everything here is re-downloadable
    from the registry and verified against the manifest on arrival, so losing it
    costs bandwidth rather than state."""
    if override:
        return Path(override)
    return Path.home() / ".cache" / "bot-bottle" / "artifacts"


def host_firecracker_cache_dir() -> Path:
    """The firecracker backend's cache root (kernel, rootfs, run dirs), created
    on demand. `BOT_BOTTLE_FC_CACHE` relocates it, like `BOT_BOTTLE_ROOT`."""
    d = Path(os.environ.get(
        "BOT_BOTTLE_FC_CACHE", str(Path.home() / ".cache" / "bot-bottle" / "firecracker"),
    ))
    d.mkdir(parents=True, exist_ok=True)
    return d


__all__ = [
    "HOST_DB_FILENAME",
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
