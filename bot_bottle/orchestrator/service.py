"""The per-host orchestrator core (PRD 0070).

`Orchestrator` is the single backend-neutral object the control plane talks
to: it owns the registry (runtime state) and brokers agent launches. It
never branches on backend — the `LaunchBroker` abstracts the backend-native
launch, so this same object drives docker / firecracker / apple once a real
broker is wired in.

Launch lifecycle:

  * `launch_bottle` mints the bottle (registry: source IP + identity
    token), sends a *signed, structured* launch request through the broker,
    and returns the record. If the broker rejects/fails, the registry entry
    is rolled back so a failed launch leaves no orphan.
  * `teardown_bottle` sends a signed teardown request, then deregisters.
"""

from __future__ import annotations

from .broker import LaunchBroker, LaunchRequest, sign_request
from .registry import BottleRecord, RegistryStore
from .sidecar import Sidecar


class Orchestrator:
    """Owns the registry + brokers launches, and manages the single
    consolidated per-host sidecar. Backend-neutral (broker and sidecar
    abstract the backend-native pieces)."""

    def __init__(
        self,
        registry: RegistryStore,
        broker: LaunchBroker,
        sign_secret: bytes,
        sidecar: Sidecar | None = None,
    ) -> None:
        self.registry = registry
        self._broker = broker
        self._secret = sign_secret
        self._sidecar = sidecar

    def launch_bottle(
        self,
        source_ip: str,
        *,
        image_ref: str = "",
        slot: int | None = None,
        metadata: str = "",
    ) -> BottleRecord:
        """Register a bottle and broker its launch. Rolls the registry entry
        back if the launch doesn't take, so a failure leaves no orphan."""
        rec = self.registry.register(source_ip, metadata=metadata)
        req = LaunchRequest(
            op="launch",
            bottle_id=rec.bottle_id,
            source_ip=source_ip,
            image_ref=image_ref,
            slot=slot,
        )
        launched = False
        try:
            self._broker.submit(sign_request(req, self._secret))
            launched = True
        finally:
            if not launched:
                self.registry.deregister(rec.bottle_id)
        return rec

    def teardown_bottle(self, bottle_id: str) -> bool:
        """Broker teardown then deregister. False if the bottle is unknown."""
        rec = self.registry.get(bottle_id)
        if rec is None:
            return False
        req = LaunchRequest(op="teardown", bottle_id=bottle_id, source_ip=rec.source_ip)
        self._broker.submit(sign_request(req, self._secret))
        self.registry.deregister(bottle_id)
        return True

    def attribute(self, source_ip: str, identity_token: str) -> BottleRecord | None:
        """Fail-closed attribution (delegates to the registry)."""
        return self.registry.attribute(source_ip, identity_token)

    # --- consolidated sidecar ----------------------------------------------

    def ensure_sidecar(self) -> None:
        """Ensure the single per-host sidecar is built and up (idempotent).
        No-op when no sidecar is configured."""
        if self._sidecar is not None:
            self._sidecar.ensure_built()
            self._sidecar.ensure_running()

    def sidecar_status(self) -> dict[str, object]:
        """Report the shared sidecar for the control plane / console."""
        if self._sidecar is None:
            return {"configured": False}
        return {
            "configured": True,
            "name": self._sidecar.name,
            "running": self._sidecar.is_running(),
        }


__all__ = ["Orchestrator"]
