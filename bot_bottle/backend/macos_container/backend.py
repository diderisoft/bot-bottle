"""MacosContainerBottleBackend — Apple Container implementation."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Sequence

from ...agent_provider import AgentProvisionPlan
from ...egress import EgressPlan
from ...env import ResolvedEnv
from ...git_gate import GitGatePlan
from ...supervise import SupervisePlan
from ...manifest import Manifest
from .. import ActiveAgent, BottleBackend, BottleImages, BottleSpec
from . import cleanup as _cleanup
from . import enumerate as _enumerate
from . import launch as _launch
from . import resolve_plan as _resolve_plan
from . import util as _container
from .bottle import MacosContainerBottle
from .bottle_cleanup_plan import MacosContainerBottleCleanupPlan
from .bottle_plan import MacosContainerBottlePlan


class MacosContainerBottleBackend(
    BottleBackend["MacosContainerBottlePlan", "MacosContainerBottleCleanupPlan"]
):
    """Apple Container backend. Selected by
    `BOT_BOTTLE_BACKEND=macos-container` or
    `--backend=macos-container`."""

    name = "macos-container"

    @classmethod
    def is_available(cls) -> bool:
        return _container.is_available()

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
    ) -> MacosContainerBottlePlan:
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

    def prelaunch_checks(self, plan: MacosContainerBottlePlan) -> None:
        _launch.stale_checks(plan)

    def _build_or_load_images(self, plan: MacosContainerBottlePlan) -> BottleImages:
        return _launch.build_or_load_images(plan)

    @contextmanager
    def _launch_impl(
        self, plan: MacosContainerBottlePlan, images: BottleImages
    ) -> Generator[MacosContainerBottle, None, None]:
        with _launch.launch(plan, images, provision=self.provision) as bottle:
            yield bottle

    def ensure_orchestrator(self) -> str:
        """Bring up the per-host infra container (control plane + gateway) and
        return its control-plane URL — the on-demand entry point operator tools
        (`supervise`) call when no control plane is running yet. Mirrors
        firecracker's infra-VM bring-up."""
        from .infra import MacosInfraService
        return MacosInfraService().ensure_running().control_plane_url

    def prepare_cleanup(self) -> MacosContainerBottleCleanupPlan:
        return _cleanup.prepare_cleanup()

    def cleanup(self, plan: MacosContainerBottleCleanupPlan) -> None:
        _cleanup.cleanup(plan)

    def enumerate_active(self) -> Sequence[ActiveAgent]:
        return _enumerate.enumerate_active()

    def supervise_mcp_url(self, plan: MacosContainerBottlePlan) -> str:
        return plan.agent_supervise_url
