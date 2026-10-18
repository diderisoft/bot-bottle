"""The per-host infra service for the Firecracker backend (PRD 0070).

`FirecrackerInfraService` is the Firecracker `InfraService`: it composes the
orchestrator + gateway microVMs as an idempotent per-host singleton pair over
the shared `infra_vm` substrate (boot primitives, the singleton flock, the
adopt/version machinery). Unlike the docker/macOS services it takes no
per-instance config — the firecracker pair is a **hard** per-host singleton,
pinned to the fixed netpool links (`orch_slot()` / `gw_slot()`), host cache
paths, and a `booted-version` marker, so two pairs can't coexist on one host.
"""

from __future__ import annotations

from ...log import die, info
from ...image_registry import RegistryError
from ...published_artifacts import ArtifactsManifestError
from ..infra_service import InfraService
from . import infra_vm
from .gateway import FirecrackerGateway
from .orchestrator import FirecrackerOrchestrator
from .gateway_attach import FirecrackerGatewayAttacher


class FirecrackerInfraService(InfraService):
    """Bring up the orchestrator + gateway microVM pair as a per-host singleton."""

    name = "firecracker"

    def orchestrator(self) -> FirecrackerOrchestrator:
        """The control-plane service (adopts the running orchestrator VM by its
        fixed link; no live handle needed for URL / SSH / health)."""
        return FirecrackerOrchestrator()

    def gateway(self) -> FirecrackerGateway:
        """The data-plane service (adopts the running gateway VM by its fixed
        link; CA fetch / provisioning go through the stable key)."""
        return FirecrackerGateway()

    def _bring_up(self, *, startup_timeout: float) -> tuple[str, bool]:
        """Idempotent per-host singleton pair. Adopt the running VMs if the
        orchestrator's control plane is healthy AND the gateway VM is alive AND
        both booted the CURRENT code version (a prior launcher booted them — they
        outlive short-lived `start` processes); otherwise clear any stale VMs and
        boot a fresh pair (orchestrator first, then the gateway that resolves
        policy against it). Returns the host control-plane URL and whether it
        booted the pair (True) or adopted it (False).

        Concurrency-safe: the cold stop/build/boot path is serialized by a host
        flock, so two simultaneous first launches don't both boot on the same
        links/PIDs. The healthy fast-path takes no lock."""
        key = infra_vm._infra_dir() / "id_ed25519"
        orchestrator = self.orchestrator()
        url = orchestrator.url()
        # Resolving the expected version reads the published manifest; a missing
        # or malformed one, or an unresolvable image, should surface as a clean
        # one-line error with remediation, not a traceback out of a launch.
        try:
            want = infra_vm.expected_version()
        except (RegistryError, ArtifactsManifestError) as e:
            die(str(e))
        if infra_vm.adoptable(key, url, want):
            info(f"adopting running infra VMs (orchestrator at {url})")
            return url, False

        with infra_vm.singleton_lock():
            # Re-check under the lock: another launcher may have booted them while
            # we waited (double-checked, so we adopt not re-boot).
            if infra_vm.adoptable(key, url, want):
                info(f"adopting running infra VMs (orchestrator at {url})")
                return url, False
            # Clear stale/hung/OUTDATED VMs holding either link before booting fresh.
            infra_vm.stop()
            # Fetch + verify the rootfs artifacts; a 404, digest mismatch, or
            # unreachable registry is a clean fail-closed message, not a stack
            # trace (matches the behavior of the retired infra_artifact.py).
            try:
                infra_vm.ensure_built()
            except (RegistryError, ArtifactsManifestError) as e:
                die(str(e))
            # Orchestrator first — the gateway daemons reach the control plane at
            # startup. Each service owns its own boot; the orchestrator (which
            # holds the signing key) mints the role-scoped `gateway` JWT for the
            # gateway, which never sees the key (#469).
            orchestrator.ensure_running(startup_timeout=startup_timeout)
            gateway = self.gateway()
            gateway.connect_to_orchestrator(
                orchestrator.gateway_url(), orchestrator.mint_gateway_token())
            infra_vm.record_booted_version(want)
            # Both VMs just booted: the gateway has no per-bottle state and the
            # orchestrator holds no egress tokens.
            return url, True

    def stop(self) -> None:
        """Stop both infra VMs (idempotent) and drop the version marker."""
        infra_vm.stop()

    def gateway_attacher(self) -> FirecrackerGatewayAttacher:
        return FirecrackerGatewayAttacher()


__all__ = ["FirecrackerInfraService"]
