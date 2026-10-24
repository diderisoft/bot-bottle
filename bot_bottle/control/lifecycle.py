"""The backend-neutral `Orchestrator` service and shared lifecycle pieces: the
control-plane port, startup timeout, start error and `source_hash`.
Implementations are in `backend/*/orchestrator.py`.
"""

from __future__ import annotations

import abc
import hashlib
import urllib.error
import urllib.request
from pathlib import Path

from ..image_inputs import package_files
from ..trust_domain import ControlPlaneProvisioning, HostControlProvisioning, HostLink

DEFAULT_PORT = 8099
DEFAULT_STARTUP_TIMEOUT_SECONDS = 45.0
# One /health probe's timeout, short so a wedged control plane fails fast.
DEFAULT_HEALTH_TIMEOUT_SECONDS = 1.0


class OrchestratorStartError(RuntimeError):
    """The control plane didn't become healthy in time."""


def source_hash(repo_root: Path) -> str:
    """Hash of the package slice the orchestrator image bakes. Backends
    recreate the container on a mismatch and leave a current one alone (keeping
    its egress tokens)."""
    h = hashlib.sha256()
    for path in package_files("bot-bottle-orchestrator", repo_root):
        if path.suffix != ".py":
            continue
        h.update(str(path.relative_to(repo_root)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


class Orchestrator(abc.ABC):
    """The per-host orchestrator service, one impl per backend. Sole holder of
    the signing key and sole opener of `bot-bottle.db`, so it mints the
    gateway's token."""

    # Control-plane auth provisioning; never starts the orchestrator without
    # its key.
    provisioning: ControlPlaneProvisioning = ControlPlaneProvisioning()
    # Set in the host controller's process so orchestrators it starts can reach
    # it; None elsewhere.
    host_control: HostControlProvisioning | None = None

    def ensure_built(self) -> None:
        """Build the image or rootfs if needed. Default: nothing to build."""
        return

    @abc.abstractmethod
    def ensure_running(
        self, *, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> bool:
        """Start the control plane unless it's healthy on current source, then
        wait for `/health`. Returns True when (re)started (its egress tokens
        are gone), False when adopted. Raises `OrchestratorStartError` on
        timeout."""

    @abc.abstractmethod
    def is_running(self) -> bool:
        """True iff the orchestrator is up."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Remove the orchestrator. Idempotent."""

    @abc.abstractmethod
    def url(self) -> str:
        """The control-plane URL the CLI uses."""

    @abc.abstractmethod
    def gateway_url(self) -> str:
        """The URL the gateway resolves policy against; docker uses the
        container name."""

    def is_healthy(self, *, timeout: float = DEFAULT_HEALTH_TIMEOUT_SECONDS) -> bool:
        """True iff `/health` at `url()` answers 200."""
        return self.health_problem(timeout=timeout) is None

    def health_problem(self, *, timeout: float = DEFAULT_HEALTH_TIMEOUT_SECONDS) -> str | None:
        """None if `/health` at `url()` answers 200, else why not."""
        url = self.url()
        if not url:
            return "it has no address yet"
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=timeout) as resp:
                return None if resp.status == 200 else f"{url}/health answered {resp.status}"
        except urllib.error.URLError as e:
            return f"{url}/health: {e.reason}"
        except (TimeoutError, OSError) as e:
            return f"{url}/health: {e}"

    def mint_gateway_token(self) -> str:
        """Mint the gateway's `gateway` token from the host signing key."""
        return self.provisioning.gateway_token()

    def host_link(self) -> HostLink | None:
        """The host's address on this orchestrator's network, or None."""
        return None

    def host_control_env(self) -> dict[str, str]:
        """The host controller's URL and `orchestrator` token, when it started
        this."""
        provisioning = self.host_control
        link = self.host_link() if provisioning is not None else None
        if provisioning is None or link is None:
            return {}
        return provisioning.orchestrator_env(link)

    def serve_host_control(self) -> None:
        """Have the host controller listen on this orchestrator's link."""
        provisioning = self.host_control
        link = self.host_link() if provisioning is not None else None
        if provisioning is not None and link is not None:
            provisioning.listen(link)

    def control_plane_key(self) -> str:
        """The raw signing key the control-plane process receives. Raises
        rather than yield an empty key that would serve open."""
        return self.provisioning.orchestrator_key()


__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_STARTUP_TIMEOUT_SECONDS",
    "DEFAULT_HEALTH_TIMEOUT_SECONDS",
    "OrchestratorStartError",
    "source_hash",
    "Orchestrator",
    "ControlPlaneProvisioning",
    "HostControlProvisioning",
    "HostLink",
]
