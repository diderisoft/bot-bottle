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


class Orchestrator:
    """Owns the registry + brokers launches. Backend-neutral."""

    def __init__(
        self, registry: RegistryStore, broker: LaunchBroker, sign_secret: bytes
    ) -> None:
        self.registry = registry
        self._broker = broker
        self._secret = sign_secret

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


__all__ = ["Orchestrator"]
