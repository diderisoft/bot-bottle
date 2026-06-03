"""SmolmachinesBottleBackend — the smolmachines implementation of
BottleBackend (PRD 0023).

Per PRD 0050 the per-provider provisioning steps (prompt, skills,
the declarative provision-plan apply, supervise MCP registration)
live on the `AgentProvider` plugin under `bot_bottle/contrib/`. The
smolmachines backend only owns the steps that are about backend
infrastructure: CA install (no-op for now), workspace, git copy-in."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Sequence

from .. import ActiveAgent, Bottle, BottleBackend, BottleSpec
from . import cleanup as _cleanup
from . import enumerate as _enumerate
from . import launch as _launch
from . import prepare as _prepare
from . import smolvm as _smolvm
from .bottle import SmolmachinesBottle
from .bottle_cleanup_plan import SmolmachinesBottleCleanupPlan
from .bottle_plan import SmolmachinesBottlePlan
from .provision import ca as _ca
from .provision import git as _git
from .provision import workspace as _workspace


class SmolmachinesBottleBackend(
    BottleBackend["SmolmachinesBottlePlan", "SmolmachinesBottleCleanupPlan"]
):
    """smolmachines backend. Selected by
    `BOT_BOTTLE_BACKEND=smolmachines`."""

    name = "smolmachines"

    @classmethod
    def is_available(cls) -> bool:
        """`smolvm` on PATH. The backend additionally needs macOS
        for libkrun + TSI, but `enumerate_active` / `cleanup` are
        host-shell ops that gracefully no-op on Linux too — the
        runtime check happens at `prepare`."""
        return _smolvm.is_available()

    def _resolve_plan(
        self, spec: BottleSpec, *, stage_dir: Path
    ) -> SmolmachinesBottlePlan:
        return _prepare.resolve_plan(spec, stage_dir=stage_dir)

    @contextmanager
    def launch(
        self, plan: SmolmachinesBottlePlan
    ) -> Generator[SmolmachinesBottle, None, None]:
        with _launch.launch(plan, provision=self.provision) as bottle:
            yield bottle

    def provision_ca(
        self, plan: SmolmachinesBottlePlan, bottle: Bottle
    ) -> None:
        _ca.provision_ca(plan, bottle)

    def provision_workspace(
        self, plan: SmolmachinesBottlePlan, bottle: Bottle
    ) -> None:
        _workspace.provision_workspace(plan, bottle)

    def provision_git(
        self, plan: SmolmachinesBottlePlan, bottle: Bottle
    ) -> None:
        _git.provision_git(plan, bottle)

    def supervise_mcp_url(self, plan: SmolmachinesBottlePlan) -> str:
        """The smolmachines guest reaches the supervise sidecar via a
        host-published random port the launch step pinned earlier
        (`http://<loopback_ip>:<random_port>/`). `agent_supervise_url`
        on the plan is "" when the bottle has no sidecar."""
        return plan.agent_supervise_url

    def prepare_cleanup(self) -> SmolmachinesBottleCleanupPlan:
        return _cleanup.prepare_cleanup()

    def cleanup(self, plan: SmolmachinesBottleCleanupPlan) -> None:
        _cleanup.cleanup(plan)

    def enumerate_active(self) -> Sequence[ActiveAgent]:
        return _enumerate.enumerate_active()
