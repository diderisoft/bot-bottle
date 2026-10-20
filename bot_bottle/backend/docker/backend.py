"""DockerBottleBackend — the Docker implementation of BottleBackend.

This module is a thin façade. The real work lives in four siblings:

  - resolve_plan.py — Docker-specific resolution into a DockerBottlePlan
  - launch.py       — bring-up + teardown context manager
  - cleanup.py      — orphan enumeration + removal
  - enumerate.py    — active-agent listing

The base class's `prepare` template runs cross-backend host-side
validation before calling `_resolve_plan` here.

Per PRD 0050 the per-provider provisioning steps (prompt, skills,
the declarative provision-plan apply, supervise MCP registration)
live on the `AgentProvider` plugin under `bot_bottle/contrib/`. The
Docker backend only owns the steps that are about backend
infrastructure: CA install and git copy-in.
"""

from __future__ import annotations

import shutil
import io
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from typing import TYPE_CHECKING, Generator, Sequence

from ...contract.supervise import SUPERVISE_HOSTNAME, SUPERVISE_PORT
from ...agent_provider import AgentProvisionPlan
from ...egress import EgressPlan
from ...env import ResolvedEnv
from ...git_gate import GitGatePlan
from ...supervisor.plan import SupervisePlan
from ...manifest import Manifest
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
    """Docker backend implementation. Selected by BOT_BOTTLE_BACKEND
    when set to `docker`; retained as a legacy/example backend."""

    name = "docker"

    @classmethod
    def is_available(cls) -> bool:
        """`docker` on PATH is sufficient; we don't probe `docker info`
        eagerly because the cross-backend enumerator runs this on
        every `list active` and we'd pay a subprocess per call. A
        broken daemon will surface its own error during prepare /
        launch."""
        return shutil.which("docker") is not None

    @classmethod
    def setup(cls) -> int:
        from . import setup as _setup
        return _setup.setup()

    @classmethod
    def status(cls, *, quiet: bool = False) -> int:
        from . import setup as _setup
        if quiet:
            with redirect_stderr(io.StringIO()):
                return _setup.status()
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
        return DockerInfraService()

    def supervise_mcp_url(self, plan: DockerBottlePlan) -> str:
        """Docker bottles reach the supervise daemon via the
        compose-network alias `supervise:9100`. No per-bottle URL
        plumbing needed; the alias resolves inside the bridge."""
        if plan.supervise_plan is None:
            return ""
        # Consolidated: the supervise daemon lives on the shared gateway, so
        # the agent registers the gateway address (NO_PROXY bypasses egress),
        # not the per-bottle `supervise` alias.
        if plan.agent_supervise_url:
            return plan.agent_supervise_url
        return f"http://{SUPERVISE_HOSTNAME}:{SUPERVISE_PORT}/"

    def prepare_cleanup(self) -> DockerBottleCleanupPlan:
        return _cleanup.prepare_cleanup()

    def cleanup(self, plan: DockerBottleCleanupPlan) -> None:
        _cleanup.cleanup(plan)

    def enumerate_active(self) -> Sequence[ActiveAgent]:
        return _enumerate.enumerate_active()
