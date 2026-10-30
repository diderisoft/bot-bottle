"""The backend-neutral `Gateway` service: one persistent gateway per host,
shared by every bottle and attributing each request by (source IP, identity
token). `connect_to_orchestrator` is an idempotent singleton bring-up.
Implementations are in `backend/*/gateway.py`.
"""

from __future__ import annotations

import abc
from pathlib import Path
from typing import Protocol

from ..environment import resource_prefix
from ..contract.hostctl import DEFAULT_GATEWAY_IMAGE
from ..paths import host_gateway_ca_dir

# mitmproxy writes its CA shortly after start, so reads poll.
CA_POLL_SECONDS = 0.5
DEFAULT_CA_TIMEOUT_SECONDS = 30.0

GATEWAY_NAME = resource_prefix() + "orch-gateway"
GATEWAY_LABEL = f"{GATEWAY_NAME}=1"
# The shared network for the gateway and every agent; the source IP the gateway
# attributes by is the address here.
GATEWAY_NETWORK = resource_prefix() + "gateway"

# mitmproxy's CA dir. The host CA dir is mounted here so the CA survives
# recreation (a named volume could be pruned).
MITMPROXY_HOME = "/home/mitmproxy/.mitmproxy"
GATEWAY_CA_CERT = f"{MITMPROXY_HOME}/mitmproxy-ca-cert.pem"

# CA files mitmproxy writes into its confdir and reuses on start.
# `mitmproxy-ca.pem` (cert + key) is the signing identity.
GATEWAY_CA_GLOB = "mitmproxy-ca*"

# A python with a known PATH: exec'd commands get no login shell.
_GATEWAY_PYTHON = (
    "/usr/bin/env", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "python3",
)

# Default gateway image (launches use `InfraSettings.gateway_image`) and its
# Dockerfile.
GATEWAY_IMAGE = DEFAULT_GATEWAY_IMAGE
GATEWAY_DOCKERFILE = "Dockerfile.gateway"


def rotate_gateway_ca(ca_dir: Path | None = None) -> list[Path]:
    """Delete the persisted CA so the next gateway start mints a fresh one.
    Returns the files removed; idempotent. Doesn't restart the gateway or
    re-provision agents."""
    ca_dir = ca_dir if ca_dir is not None else host_gateway_ca_dir()
    removed: list[Path] = []
    for path in sorted(ca_dir.glob(GATEWAY_CA_GLOB)):
        path.unlink()
        removed.append(path)
    return removed


class GatewayError(Exception):
    """The gateway failed to build, start or stop."""


class GatewayProvisionError(RuntimeError):
    """A git-gate provisioning step in the gateway failed."""


class GatewayTransport(Protocol):
    """Runs commands and copies files into the running gateway: docker or
    `container` exec/cp, or SSH for firecracker."""

    def exec(self, argv: list[str]) -> None:
        """Run `argv` in the gateway; raises `GatewayProvisionError` on
        failure."""

    def cp_into(self, src: str, dest: str) -> None:
        """Copy host file `src` to `dest` in the gateway; raises on failure."""


class Gateway(abc.ABC):
    """The per-host gateway service, one impl per backend. It never holds the
    signing key, only the `gateway` token the orchestrator mints."""

    name: str

    def ensure_built(self) -> None:
        """Build the gateway's image or rootfs if needed. Default: nothing to
        build."""
        return

    @abc.abstractmethod
    def connect_to_orchestrator(self, orchestrator_url: str, gateway_token: str) -> bool:
        """Bind to this orchestrator (URL and `gateway` token) and bring the
        gateway up with the CA. A healthy, current gateway on the same binding
        is left alone. Returns True when (re)started, so it lost its per-bottle
        state; False when adopted."""

    @abc.abstractmethod
    def is_running(self) -> bool:
        """True iff the gateway is up."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Remove the gateway. Idempotent."""

    @abc.abstractmethod
    def address(self) -> str:
        """The agent-facing address for egress, git-http and supervise."""

    @abc.abstractmethod
    def ca_cert_pem(self, *, timeout: float = DEFAULT_CA_TIMEOUT_SECONDS) -> str:
        """The CA PEM agents install. Polls."""

    @abc.abstractmethod
    def persisted_ca_dir(self) -> Path | None:
        """The host dir the CA persists in, or None when it isn't one this
        process can clear (a named volume)."""

    def roll_over_ca(self) -> None:
        """Replace the running gateway's CA with a new one cross-signed by the
        current one, effective on its next start. Raises `GatewayError`."""
        try:
            self.provisioning_transport().exec(
                [*_GATEWAY_PYTHON, "-m", "bot_bottle.gateway.ca_rollover", MITMPROXY_HOME])
        except GatewayProvisionError as e:
            raise GatewayError(f"could not roll the gateway CA over: {e}") from e
        self._save_rolled_over_ca()

    def _save_rolled_over_ca(self) -> None:
        """Copy the rolled-over CA to where it persists. Default: nothing to
        do."""

    @abc.abstractmethod
    def provisioning_transport(self) -> "GatewayTransport":
        """The exec/cp transport for git-gate provisioning."""


__all__ = [
    "Gateway", "GatewayError", "GatewayProvisionError", "GatewayTransport",
    "rotate_gateway_ca",
    "GATEWAY_NAME", "GATEWAY_LABEL", "GATEWAY_IMAGE", "GATEWAY_NETWORK",
    "GATEWAY_CA_CERT", "GATEWAY_CA_GLOB",
]
