"""The per-host infra service contract (PRD 0070).

`InfraService` is the backend-neutral composer of the per-host **pair**: the
`Orchestrator` (control plane) + `Gateway` (data plane) services, brought up as
an idempotent per-host singleton. One concrete impl per backend
(`backend/*/infra.py`), mirroring how `Orchestrator` and `Gateway` each have a
per-backend impl.

The two backend-specific accessors (`orchestrator()` / `gateway()`) are the
source of truth for how to reach each plane; the convenience `url()` /
`is_healthy()` just delegate to the orchestrator.
"""

from __future__ import annotations

import abc

from ..gateway import Gateway
from ..log import die
from ..orchestrator.lifecycle import DEFAULT_STARTUP_TIMEOUT_SECONDS, Orchestrator
from .gateway_attach import GatewayAttachError, GatewayAttacher, reattach_running_bottles


class InfraService(abc.ABC):
    """Compose + bring up the per-host orchestrator + gateway pair.

    Backend-neutral: docker/macOS run the pair as two containers, firecracker as
    two microVMs. Callers read reach-info off `orchestrator()` / `gateway()`."""

    @abc.abstractmethod
    def orchestrator(self) -> Orchestrator:
        """The control-plane service. Cheap to reconstruct."""

    @abc.abstractmethod
    def gateway(self) -> Gateway:
        """The data-plane service. Cheap to reconstruct."""

    @abc.abstractmethod
    def ensure_running(
        self, *, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> str:
        """Bring the orchestrator + gateway pair up (idempotent per-host
        singleton — a healthy, current pair is left running). Returns the host
        control-plane URL. Raises `OrchestratorStartError` on control-plane
        startup timeout."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Remove both the orchestrator and the gateway. Idempotent."""

    @abc.abstractmethod
    def gateway_attacher(self) -> GatewayAttacher:
        """How this backend lists its running bottles and reattaches one to
        the gateway (PRD 0081)."""

    def reattach_running_bottles(self, url: str) -> None:
        """Restore every running bottle's CA, git-gate state and egress tokens
        after the gateway or orchestrator came up cold (PRD 0081). Called by
        `ensure_running` on that path only, never when it adopted a running
        pair. Any bottle that can't be reattached ends the bring-up, naming
        each one."""
        try:
            reattach_running_bottles(
                self.gateway_attacher(), orchestrator_url=url, gateway=self.gateway())
        except GatewayAttachError as e:
            die(str(e))

    def url(self) -> str:
        """The host-facing control-plane URL the CLI reaches (the orchestrator's)."""
        return self.orchestrator().url()

    def is_healthy(self) -> bool:
        """True iff the control plane answers `/health`."""
        return self.orchestrator().is_healthy()


__all__ = ["InfraService"]
