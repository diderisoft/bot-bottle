"""`DockerBottleBackend`: a facade over `resolve_plan`, `launch`, `cleanup` and
`enumerate`.
"""

from __future__ import annotations

import shutil
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
from .bottle import DockerBottle
from .bottle_cleanup_plan import DockerBottleCleanupPlan
from .bottle_plan import DockerBottlePlan

if TYPE_CHECKING:
    from .infra import DockerInfraService
class DockerBottleBackend(BottleBackend["DockerBottlePlan", "DockerBottleCleanupPlan"]):
    """The docker backend, selected with `BOT_BOTTLE_BACKEND=docker`."""

    name = "docker"

    @classmethod
    def is_available(cls) -> bool:
        """`docker` on PATH. The daemon isn't probed, since cross-backend
        listing calls this often; a broken daemon errors at prepare or launch."""
        return shutil.which("docker") is not None

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
    ) -> DockerBottlePlan:
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

    def prelaunch_checks(self, plan: DockerBottlePlan) -> None:
        _launch.stale_checks(plan)

    def _build_or_load_images(self, plan: DockerBottlePlan) -> BottleImages:
        return _launch.build_or_load_images(plan)

    @contextmanager
    def _launch_impl(self, plan: DockerBottlePlan, images: BottleImages) -> Generator[DockerBottle, None, None]:
        with _launch.launch(plan, images, provision=self.provision) as bottle:
            yield bottle

    def infra_service(self) -> "DockerInfraService":
        from .infra import DockerInfraService
        return DockerInfraService(self.settings)

    def prepare_cleanup(self) -> DockerBottleCleanupPlan:
        return _cleanup.prepare_cleanup(self.settings)

    def cleanup(self, plan: DockerBottleCleanupPlan) -> None:
        _cleanup.cleanup(plan, self.settings)

    def enumerate_active(self) -> Sequence[ActiveAgent]:
        return _enumerate.enumerate_active()
