"""FirecrackerBottleBackend — Linux microVM implementation.

Replaces smolmachines on Linux (issue #342): mature KVM-based
isolation via Firecracker, SSH control over a point-to-point TAP, and a
fail-closed nftables egress boundary. Selected by
`BOT_BOTTLE_BACKEND=firecracker` or `--backend=firecracker`.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Sequence

from ...agent_provider import AgentProvisionPlan
from ...egress import EgressPlan
from ...env import ResolvedEnv
from ...git_gate import GitGatePlan
from ...manifest import Manifest
from ...supervise import SupervisePlan
from .. import ActiveAgent, BottleBackend, BottleImages, BottleSpec
from . import cleanup as _cleanup
from . import enumerate as _enumerate
from . import launch as _launch
from . import resolve_plan as _resolve_plan
from . import util as _util
from .bottle import FirecrackerBottle
from .bottle_cleanup_plan import FirecrackerBottleCleanupPlan
from .bottle_plan import FirecrackerBottlePlan


class FirecrackerBottleBackend(
    BottleBackend["FirecrackerBottlePlan", "FirecrackerBottleCleanupPlan"]
):
    name = "firecracker"

    @classmethod
    def is_available(cls) -> bool:
        return _util.is_available()

    @classmethod
    def is_host_capable(cls) -> bool:
        """Linux + KVM, regardless of whether the `firecracker` binary
        is installed. Drives default-backend selection so a capable host
        without the binary still lands on firecracker and gets an
        install pointer at launch."""
        return _util.is_host_capable()

    @classmethod
    def setup(cls) -> int:
        from . import setup as _setup
        return _setup.setup()

    @classmethod
    def status(cls) -> int:
        from . import setup as _setup
        return _setup.status()

    @classmethod
    def teardown(cls) -> int:
        from . import setup as _setup
        return _setup.teardown()

    def _preflight(self) -> None:
        _resolve_plan.preflight()

    def _build_guest_env(self, resolved_env: ResolvedEnv) -> dict[str, str]:
        return _resolve_plan.build_guest_env(resolved_env)

    def _resolve_plan(
        self,
        spec: BottleSpec,
        *,
        manifest: Manifest,
        slug: str,
        resolved_env: ResolvedEnv,
        agent_provision_plan: AgentProvisionPlan,
        egress_plan: EgressPlan,
        git_gate_plan: GitGatePlan,
        supervise_plan: SupervisePlan | None,
        stage_dir: Path,
    ) -> FirecrackerBottlePlan:
        return _resolve_plan.resolve_plan(
            spec,
            manifest=manifest,
            slug=slug,
            resolved_env=resolved_env,
            agent_provision_plan=agent_provision_plan,
            egress_plan=egress_plan,
            supervise_plan=supervise_plan,
            git_gate_plan=git_gate_plan,
            stage_dir=stage_dir,
        )

    def _build_or_load_images(self, plan: FirecrackerBottlePlan) -> BottleImages:
        # Firecracker builds its rootfs image inside _launch_impl (via buildah in
        # a builder VM), so there is nothing to pre-resolve here.
        return BottleImages(agent=plan.image)

    @contextmanager
    def _launch_impl(
        self, plan: FirecrackerBottlePlan, images: BottleImages,
    ) -> Generator[FirecrackerBottle, None, None]:
        del images  # rootfs built inside _launch.launch; images unused here
        with _launch.launch(plan, provision=self.provision) as bottle:
            yield bottle

    def prepare_cleanup(self) -> FirecrackerBottleCleanupPlan:
        return _cleanup.prepare_cleanup()

    def cleanup(self, plan: FirecrackerBottleCleanupPlan) -> None:
        _cleanup.cleanup(plan)

    def enumerate_active(self) -> Sequence[ActiveAgent]:
        return _enumerate.enumerate_active()

    def supervise_mcp_url(self, plan: FirecrackerBottlePlan) -> str:
        return plan.agent_supervise_url

    def ensure_orchestrator(self) -> str:
        from . import infra_vm
        return infra_vm.ensure_running().control_plane_url
