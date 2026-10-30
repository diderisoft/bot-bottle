"""`FirecrackerInfraService`: the orchestrator and gateway VMs as a hard
per-host singleton, pinned to the fixed netpool links, host cache paths and a
`booted-version` marker.
"""

from __future__ import annotations

from ...contract.hostctl import Settings
from ...lib.log import die, info
from ...image_registry import RegistryError
from ...published_artifacts import FORMAT_EXT4, ArtifactsManifestError
from ..infra_service import InfraService
from . import infra_vm
from .gateway import FirecrackerGateway
from .orchestrator import FirecrackerOrchestrator
from .gateway_attach import FirecrackerGatewayAttacher


class FirecrackerInfraService(InfraService):
    """Bring up the orchestrator + gateway VM pair as a per-host singleton."""

    name = "firecracker"
    infra_format = FORMAT_EXT4

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def orchestrator(self) -> FirecrackerOrchestrator:
        """The control-plane service; adopts the running VM by its fixed link."""
        return FirecrackerOrchestrator(self.settings)

    def gateway(self) -> FirecrackerGateway:
        """The gateway service; adopts the running VM by its fixed link."""
        return FirecrackerGateway(self.settings)

    def _bring_up(self, *, startup_timeout: float) -> tuple[str, bool]:
        """Adopt the running pair if the orchestrator is healthy, the gateway
        VM is alive and both booted the current version; otherwise stop any
        stale VMs and boot a fresh pair, orchestrator first. Returns the
        control-plane URL and whether it booted (True) or adopted (False).

        The cold path holds a host flock; the healthy fast path takes no lock."""
        key = infra_vm._infra_dir() / "id_ed25519"
        orchestrator = self.orchestrator()
        url = orchestrator.url()
        # A missing manifest or unresolvable image is a clean error, not a
        # traceback.
        try:
            want = infra_vm.expected_version(self.settings.infra)
        except (RegistryError, ArtifactsManifestError) as e:
            die(str(e))
        if infra_vm.adoptable(key, url, want):
            info(f"adopting running infra VMs (orchestrator at {url})")
            return url, False

        with infra_vm.singleton_lock():
            # Re-check under the lock: another launcher may have booted them.
            if infra_vm.adoptable(key, url, want):
                info(f"adopting running infra VMs (orchestrator at {url})")
                return url, False
            # Clear stale, hung or outdated VMs holding either link.
            infra_vm.stop()
            # A 404, digest mismatch or unreachable registry fails closed with
            # a clean message.
            try:
                infra_vm.ensure_built(self.settings)
            except (RegistryError, ArtifactsManifestError) as e:
                die(str(e))
            # Orchestrator first: the gateway reaches it at startup. The
            # orchestrator mints the gateway's token.
            orchestrator.ensure_running(startup_timeout=startup_timeout)
            gateway = self.gateway()
            gateway.connect_to_orchestrator(
                orchestrator.gateway_url(), orchestrator.mint_gateway_token())
            infra_vm.record_booted_version(want)
            # Both just booted, so neither holds per-bottle state.
            return url, True

    def stop(self) -> None:
        """Stop both VMs (idempotent) and drop the version marker."""
        infra_vm.stop()

    def gateway_attacher(self) -> FirecrackerGatewayAttacher:
        return FirecrackerGatewayAttacher()


__all__ = ["FirecrackerInfraService"]
