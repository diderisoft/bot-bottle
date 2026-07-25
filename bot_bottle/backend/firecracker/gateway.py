"""The Firecracker gateway data plane as a microVM (PRD 0070).

`FirecrackerGateway` is the Firecracker implementation of the backend-neutral
`Gateway` service. The gateway here is a whole microVM — the slim data-plane
unit (mitmproxy TLS bump + DLP + git-http) booted on its own NAT'd link; agent
VMs' gateway-port traffic is DNAT'd to it and it never opens `bot-bottle.db`
(#469), so it holds no signing key, only the pre-minted `gateway` token the
host hands it via `connect_to_orchestrator`.

The VM lifecycle primitives (boot, SSH secret push, netpool links, nft, PID
files) stay in `infra_vm`, which drives the orchestrator+gateway pair as a
singleton; this adapter composes them into the uniform `Gateway` surface.
"""

from __future__ import annotations

from urllib.parse import urlparse

from ...gateway import (
    DEFAULT_CA_TIMEOUT_SECONDS,
    Gateway,
    GatewayError,
    GatewayTransport,
)
from . import infra_vm

# The gateway microVM's name (the `bb_role=gateway` boot tag / run dir). Fixed
# per host — one gateway VM, shared by every agent VM.
GATEWAY_NAME = "bot-bottle-gateway"


class FirecrackerGateway(Gateway):
    """The consolidated gateway as a Firecracker microVM on the gateway link.

    The rootfs is built/downloaded by `infra_vm.ensure_built` (the ABC's
    `ensure_built` no-op here); `connect_to_orchestrator` boots the VM resolving
    policy against the orchestrator and seeds the pre-minted `gateway` token."""

    name = GATEWAY_NAME

    def __init__(self, vm: infra_vm.InfraVm | None = None) -> None:
        # The live VM handle when this process booted it; None when adapting an
        # already-running gateway (CA fetch / provisioning go through the stable
        # key + fixed link, so no live handle is needed).
        self._vm = vm
        self._orchestrator_url = ""
        self._gateway_token = ""

    def connect_to_orchestrator(self, orchestrator_url: str, gateway_token: str) -> None:
        """Boot the gateway VM resolving policy against `orchestrator_url` and
        seed `gateway_token`. The orchestrator's guest IP is parsed from the URL
        and passed on the cmdline, so the baked init stays IP-independent. Boot
        the orchestrator first — the gateway daemons reach it at startup."""
        self._orchestrator_url = orchestrator_url
        self._gateway_token = gateway_token
        if not self._orchestrator_url:
            raise GatewayError(
                "gateway requires an orchestrator URL to run "
                "(resolver-only data plane; no single-tenant fallback)"
            )
        if not self._gateway_token:
            raise GatewayError(
                "gateway requires a pre-minted `gateway` token to run "
                "(the orchestrator mints it; the gateway never holds the key)"
            )
        orchestrator_guest_ip = urlparse(self._orchestrator_url).hostname or ""
        if not orchestrator_guest_ip:
            raise GatewayError(
                f"cannot resolve orchestrator guest IP from {self._orchestrator_url!r}"
            )
        self._vm = infra_vm.boot_gateway(orchestrator_guest_ip, self._gateway_token)

    def is_running(self) -> bool:
        return infra_vm.gateway_running()

    def stop(self) -> None:
        """Stop only the gateway VM (idempotent)."""
        infra_vm.stop_gateway()

    def address(self) -> str:
        """The gateway VM's guest IP — the agent-facing target agent VMs'
        gateway-port traffic is DNAT'd to."""
        return infra_vm.gateway_guest_ip()

    def ca_cert_pem(self, *, timeout: float = DEFAULT_CA_TIMEOUT_SECONDS) -> str:
        """The gateway's mitmproxy CA (PEM) agents install to trust its TLS
        interception. Fetched from the gateway VM over SSH; polls because
        mitmproxy writes it a beat after boot."""
        vm = self._vm or infra_vm.adopted_gateway_vm()
        return vm.gateway_ca_pem(timeout=timeout)

    def provisioning_transport(self) -> GatewayTransport:
        """The exec/cp transport git-gate provisioning stages per-bottle repos +
        deploy keys through (over SSH to the gateway VM)."""
        return infra_vm.gateway_transport()


__all__ = ["FirecrackerGateway", "GATEWAY_NAME"]
