"""The host-side orchestrator (control-plane) service + shared lifecycle pieces
(PRD 0070).

`Orchestrator` is the backend-neutral host-side contract for the control plane,
mirroring `Gateway` for the data plane: build its image/rootfs, bring it up,
report where the CLI and the gateway reach it, mint the gateway's token, tear it
down. One concrete impl per backend (`backend/*/orchestrator.py`); the host
composes it with the `Gateway` service.

This module also holds the backend-neutral constants those impls build on: the
control-plane port, the startup timeout, the start-error, and the `source_hash`
used to detect a code change. (The in-guest control-plane *core* — the registry
+ broker object the running process wraps — is `service.OrchestratorCore`.)
"""

from __future__ import annotations

import abc
import hashlib
import urllib.error
import urllib.request
from pathlib import Path

from ..trust_domain import ControlPlaneProvisioning

DEFAULT_PORT = 8099
DEFAULT_STARTUP_TIMEOUT_SECONDS = 45.0
# A single /health probe's timeout (the poll loop repeats it up to the startup
# timeout). Short so a wedged control plane fails fast per attempt.
DEFAULT_HEALTH_TIMEOUT_SECONDS = 1.0


class OrchestratorStartError(RuntimeError):
    """The control plane did not become healthy within the timeout."""


def source_hash(repo_root: Path) -> str:
    """Content hash of the orchestrator's bind-mounted Python source (the
    `bot_bottle` package the control-plane process imports). Changes only
    when the code that would actually run changes — a backend's `ensure_running`
    recreates the container on a mismatch so a code change takes effect,
    but leaves a healthy up-to-date container alone to preserve in-memory
    egress tokens."""
    h = hashlib.sha256()
    for path in sorted((repo_root / "bot_bottle").rglob("*.py")):
        h.update(str(path.relative_to(repo_root)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


class Orchestrator(abc.ABC):
    """Provision + interact with the per-host orchestrator (control plane).

    One concrete impl per backend (`backend/*/orchestrator.py`); the host
    composes it with the `Gateway` service. The orchestrator is the **sole**
    holder of the signing key and the **sole** opener of `bot-bottle.db` (#469),
    so it — not the gateway — mints the gateway's role-scoped token.
    Backend-neutral."""

    # The shared control-plane auth provisioning contract (#476). Every backend
    # gets its signing key + gateway token through this one seam rather than
    # re-deriving the wiring; it is fail-closed for every backend — the
    # orchestrator never starts without its signing key.
    provisioning: ControlPlaneProvisioning = ControlPlaneProvisioning()

    def ensure_available(self) -> None:
        """Acquire the exact image/rootfs selected for this application."""
        self.ensure_built()

    def ensure_built(self) -> None:
        """Local-build implementation hook retained for backend compatibility."""
        return

    @abc.abstractmethod
    def ensure_running(
        self, *, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        """Start the control plane if it isn't already healthy on the current
        source, then block until `/health` answers. Idempotent — a healthy,
        current control plane is left running so its in-memory egress tokens
        survive (#381). Raises `OrchestratorStartError` on startup timeout."""

    @abc.abstractmethod
    def is_running(self) -> bool:
        """True iff the orchestrator instance (container / VM) is currently up."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Remove the orchestrator. Idempotent — absent is success."""

    @abc.abstractmethod
    def url(self) -> str:
        """The host-facing control-plane URL the CLI reaches the orchestrator
        at (docker: a host-loopback publish; macOS/firecracker: the guest's
        control-network / guest IP)."""

    @abc.abstractmethod
    def gateway_url(self) -> str:
        """The control-plane URL the gateway's data plane resolves policy
        against. May differ from `url()` — docker reaches the orchestrator by
        its container-DNS name on the control network, not the host loopback."""

    def is_healthy(self, *, timeout: float = DEFAULT_HEALTH_TIMEOUT_SECONDS) -> bool:
        """True iff the control plane answers 200 on `/health` at `url()`.
        Backends may override (e.g. to also check the VM process is alive)."""
        try:
            with urllib.request.urlopen(f"{self.url()}/health", timeout=timeout) as resp:
                return resp.status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def mint_gateway_token(self) -> str:
        """Mint a role-scoped `gateway` JWT from the host signing key for the
        gateway to present. The orchestrator holds the key; the gateway never
        does (#469). Routed through the shared provisioning contract (#476), so
        the same host token file is the single source of truth across backends."""
        return self.provisioning.gateway_token()

    def control_plane_key(self) -> str:
        """The raw signing key the control-plane *process* must receive — the ONE
        place a backend obtains it (docker/macOS inject it as `key_env`;
        firecracker pushes it to the guest). Fail-closed via the provisioning
        contract: it raises rather than yield an empty key that would run the
        server OPEN (#476)."""
        return self.provisioning.orchestrator_key()


__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_STARTUP_TIMEOUT_SECONDS",
    "DEFAULT_HEALTH_TIMEOUT_SECONDS",
    "OrchestratorStartError",
    "source_hash",
    "Orchestrator",
    "ControlPlaneProvisioning",
]
