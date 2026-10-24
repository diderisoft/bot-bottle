"""`MacosContainerBottleBackend`: agents in Apple containers."""

from __future__ import annotations

import io
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from typing import TYPE_CHECKING, Generator, Sequence

from ...agent_provider import AgentProvisionPlan
from ...egress import EgressPlan
from ...env import ResolvedEnv
from ...git_gate import GitGatePlan
from ...supervisor.plan import SupervisePlan
from ...manifest import Manifest
from ...secret_source import SecretSource
from .. import ActiveAgent, BottleBackend, BottleImages, BottleSpec
from . import cleanup as _cleanup
from . import enumerate as _enumerate
from . import launch as _launch
from . import resolve_plan as _resolve_plan
from . import util as _container
from .bottle import MacosContainerBottle
from .bottle_cleanup_plan import MacosContainerBottleCleanupPlan
from .bottle_plan import MacosContainerBottlePlan

if TYPE_CHECKING:
    from .infra import MacosInfraService


class MacosContainerBottleBackend(
    BottleBackend["MacosContainerBottlePlan", "MacosContainerBottleCleanupPlan"]
):
    """The Apple Container backend (`--backend=macos-container`)."""

    name = "macos-container"
    supports_nested_containers = True

    @classmethod
    def is_available(cls) -> bool:
        return _container.is_available()

    def setup(self) -> int:
        from . import setup as _setup
        return _setup.setup()

    def status(self, *, quiet: bool = False) -> int:
        from . import setup as _setup
        if quiet:
            with redirect_stderr(io.StringIO()):
                return _setup.status()
        return _setup.status()

    def teardown(self) -> int:
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
        secrets: SecretSource,
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
            settings=self.settings,
            secrets=secrets,
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

    def infra_service(self) -> "MacosInfraService":
        from .infra import MacosInfraService
        return MacosInfraService(self.settings)

    def prepare_cleanup(self) -> MacosContainerBottleCleanupPlan:
        return _cleanup.prepare_cleanup()

    def cleanup(self, plan: MacosContainerBottleCleanupPlan) -> None:
        _cleanup.cleanup(plan, self.settings)

    def enumerate_active(self) -> Sequence[ActiveAgent]:
        return _enumerate.enumerate_active()
