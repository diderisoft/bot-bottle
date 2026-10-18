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
from pathlib import Path

from ..gateway import Gateway
from ..log import die
from ..orchestrator.lifecycle import DEFAULT_STARTUP_TIMEOUT_SECONDS, Orchestrator
from ..paths import bot_bottle_root
from .gateway_attach import (
    AttachPolicy,
    GatewayAttachError,
    GatewayAttacher,
    reattach_running_bottles,
)


class InfraService(abc.ABC):
    """Compose + bring up the per-host orchestrator + gateway pair.

    Backend-neutral: docker/macOS run the pair as two containers, firecracker as
    two microVMs. Callers read reach-info off `orchestrator()` / `gateway()`.

    `ensure_running` is the same everywhere: a backend's `_bring_up` brings the
    pair up and reports whether either plane was (re)started, and only then are
    running bottles reattached (PRD 0081). Backends never decide that."""

    # Names this backend's "reattach pending" marker.
    name: str = "infra"

    @abc.abstractmethod
    def orchestrator(self) -> Orchestrator:
        """The control-plane service. Cheap to reconstruct."""

    @abc.abstractmethod
    def gateway(self) -> Gateway:
        """The data-plane service. Cheap to reconstruct."""

    def ensure_running(
        self,
        *,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        attach_policy: AttachPolicy = AttachPolicy.STRICT,
    ) -> str:
        """Bring the orchestrator + gateway pair up (idempotent per-host
        singleton — a healthy, current pair is left running) and, when either
        plane was (re)started, reattach every running bottle to it. Returns
        the host control-plane URL. Raises `OrchestratorStartError` on
        control-plane startup timeout.

        A reattach that didn't finish (it failed, or the launcher was
        interrupted) leaves a marker, so the next bring-up retries it even
        when it adopts the pair; otherwise the bottles it missed would stay
        broken until the next restart. Under the default STRICT policy a
        failure ends the bring-up naming each bottle."""
        url, restarted = self._bring_up(startup_timeout=startup_timeout)
        marker = self._reattach_pending_marker()
        if restarted or marker.exists():
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
            try:
                reattach_running_bottles(
                    self.gateway_attacher(), orchestrator_url=url,
                    gateway=self.gateway(), policy=attach_policy,
                )
            except GatewayAttachError as e:
                die(str(e))
            marker.unlink(missing_ok=True)
        return url

    @abc.abstractmethod
    def _bring_up(self, *, startup_timeout: float) -> tuple[str, bool]:
        """Bring the pair up; return the host control-plane URL and whether the
        orchestrator or the gateway was (re)started (and so lost its egress
        tokens or per-bottle state) rather than adopted."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Remove both the orchestrator and the gateway. Idempotent."""

    @abc.abstractmethod
    def gateway_attacher(self) -> GatewayAttacher:
        """How this backend lists its running bottles and reattaches one to
        the gateway (PRD 0081)."""

    def _reattach_pending_marker(self) -> Path:
        return bot_bottle_root() / "infra" / f"{self.name}.reattach-pending"

    def url(self) -> str:
        """The host-facing control-plane URL the CLI reaches (the orchestrator's)."""
        return self.orchestrator().url()

    def is_healthy(self) -> bool:
        """True iff the control plane answers `/health`."""
        return self.orchestrator().is_healthy()


__all__ = ["InfraService"]
