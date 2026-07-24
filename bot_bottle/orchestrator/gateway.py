"""The consolidated per-host gateway (PRD 0070).

The core consolidation win: **one** persistent gateway per host, shared by
every bottle, instead of a gateway per bottle. It's safe to share
because the attribution invariant (source IP + identity token, see
`registry`) lets the gateway attribute each request to the right bottle —
so per-bottle policy lives in one long-lived process keyed on who's calling.

`Gateway` is the backend-neutral lifecycle contract (mirrors `LaunchBroker`):
ensure the single instance is up, report it, tear it down. The docker
implementation (`DockerGateway`) lives in `backend/docker/gateway.py`; a
firecracker gateway VM slots in later.

The defining behaviour is **idempotent singleton**: `ensure_running` starts
the instance if absent and is a no-op if it's already up, so N bottle
launches never spawn N gateways.
"""

from __future__ import annotations

import abc
import os
from pathlib import Path

from ..paths import host_gateway_ca_dir

# The gateway's mitmproxy writes its CA a beat after the container starts, so
# reads poll for it rather than assuming it's there on a fresh launch.
CA_POLL_SECONDS = 0.5
DEFAULT_CA_TIMEOUT_SECONDS = 30.0

GATEWAY_NAME = "bot-bottle-orch-gateway"
GATEWAY_LABEL = "bot-bottle-orch-gateway=1"
# The single user-defined network the gateway and every agent bottle share.
# Agents attach here with a pinned IP and reach the gateway's egress /
# git-http / supervise ports by its address — no host port publishing, and
# the source IP the gateway attributes by is the address on this network.
GATEWAY_NETWORK = "bot-bottle-gateway"

# mitmproxy's CA dir in the bundle. The host's gateway-CA dir (see
# `host_gateway_ca_dir`) is bind-mounted here so the gateway's self-generated
# CA stays STABLE across container recreation — every agent installs this one
# CA to trust the shared gateway's TLS interception, so it must not rotate when
# the gateway restarts. A host bind-mount rather than a named volume: a named
# volume is silently wiped by `docker volume prune`, minting a fresh CA that
# breaks every running bottle (issue #450).
MITMPROXY_HOME = "/home/mitmproxy/.mitmproxy"
GATEWAY_CA_CERT = f"{MITMPROXY_HOME}/mitmproxy-ca-cert.pem"

# The CA material mitmproxy writes into its confdir. mitmproxy reuses these on
# startup when present and generates them only on first run, so persisting them
# is what makes the CA stable; deleting them (see `rotate_gateway_ca`) forces a
# fresh CA on the next start. `mitmproxy-ca.pem` (cert + private key) is the
# signing identity; the rest are derived encodings agents/clients consume.
GATEWAY_CA_GLOB = "mitmproxy-ca*"

# The gateway data-plane image + its Dockerfile. Kept as a local constant
# rather than imported from the backend layer, which would drag
# the whole backend layer into the lean orchestrator (see #359); unify when
# that lands. Env override matches the backend's BOT_BOTTLE_GATEWAY_IMAGE.
GATEWAY_IMAGE = os.environ.get("BOT_BOTTLE_GATEWAY_IMAGE", "bot-bottle-gateway:latest")
GATEWAY_DOCKERFILE = "Dockerfile.gateway"
REPO_ROOT = Path(__file__).resolve().parents[2]


def rotate_gateway_ca(ca_dir: Path | None = None) -> list[Path]:
    """Delete the persisted mitmproxy CA so the next gateway start mints a
    fresh one — the explicit, deliberate CA-rollover path (issue #450).

    Persistence keeps the CA stable across restarts precisely because mitmproxy
    reuses the on-disk CA; rotation is therefore just removing that material.
    Returns the files removed (empty when there was no CA yet); idempotent.

    This only clears the on-disk CA. It does NOT stop the running gateway (whose
    mitmproxy still holds the old CA in memory) or re-provision agents — the
    caller recreates the gateway to mint the new CA and re-attaches bottles.
    `rotate-ca` on the orchestrator CLI wires those steps together."""
    ca_dir = ca_dir if ca_dir is not None else host_gateway_ca_dir()
    removed: list[Path] = []
    for path in sorted(ca_dir.glob(GATEWAY_CA_GLOB)):
        path.unlink()
        removed.append(path)
    return removed


class GatewayError(Exception):
    """The shared gateway failed to build/start/stop (non-zero `docker` exit)."""


class Gateway(abc.ABC):
    """Lifecycle of the single per-host gateway. Backend-neutral."""

    name: str

    def ensure_built(self) -> None:
        """Ensure the gateway's image / rootfs exists, building it if needed.
        Default: nothing to build (e.g. a stub or a pre-pulled image)."""
        return

    @abc.abstractmethod
    def ensure_running(self) -> None:
        """Start the gateway if it isn't already up. Idempotent: a no-op
        when it's already running (that's the whole point — one per host).
        Assumes the image exists — call `ensure_built()` first."""

    @abc.abstractmethod
    def is_running(self) -> bool:
        """True iff the gateway instance is currently up."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Remove the gateway. Idempotent — absent is success."""


__all__ = [
    "Gateway", "GatewayError", "rotate_gateway_ca",
    "GATEWAY_NAME", "GATEWAY_LABEL", "GATEWAY_IMAGE", "GATEWAY_NETWORK",
    "GATEWAY_CA_CERT", "GATEWAY_CA_GLOB",
]
