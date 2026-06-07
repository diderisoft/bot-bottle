"""DockerBottleBackend — the Docker implementation of BottleBackend.

This module is a thin façade. The real work lives in four siblings:

  - prepare.py    — host-side resolution into a DockerBottlePlan
  - launch.py     — bring-up + teardown context manager
  - cleanup.py    — orphan enumeration + removal
  - enumerate.py  — active-agent listing

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
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Sequence

from ...supervise import SUPERVISE_HOSTNAME, SUPERVISE_PORT
from .. import ActiveAgent, Bottle, BottleBackend, BottleSpec
from . import cleanup as _cleanup
from . import enumerate as _enumerate
from . import launch as _launch
from . import prepare as _prepare
from .bottle import DockerBottle
from .bottle_cleanup_plan import DockerBottleCleanupPlan
from .bottle_plan import DockerBottlePlan
class DockerBottleBackend(BottleBackend["DockerBottlePlan", "DockerBottleCleanupPlan"]):
    """Docker backend implementation. Selected by BOT_BOTTLE_BACKEND
    (default)."""

    name = "docker"

    @classmethod
    def is_available(cls) -> bool:
        """`docker` on PATH is sufficient; we don't probe `docker info`
        eagerly because the cross-backend enumerator runs this on
        every `list active` and we'd pay a subprocess per call. A
        broken daemon will surface its own error during prepare /
        launch."""
        return shutil.which("docker") is not None

    def _resolve_plan(self, spec: BottleSpec, *, stage_dir: Path) -> DockerBottlePlan:
        return _prepare.resolve_plan(spec, stage_dir=stage_dir)

    @contextmanager
    def launch(self, plan: DockerBottlePlan) -> Generator[DockerBottle, None, None]:
        with _launch.launch(plan, provision=self.provision) as bottle:
            yield bottle

    def supervise_mcp_url(self, plan: DockerBottlePlan) -> str:
        """Docker bottles reach the supervise sidecar via the
        compose-network alias `supervise:9100`. No per-bottle URL
        plumbing needed; the alias resolves inside the bridge."""
        if plan.supervise_plan is None:
            return ""
        return f"http://{SUPERVISE_HOSTNAME}:{SUPERVISE_PORT}/"

    def prepare_cleanup(self) -> DockerBottleCleanupPlan:
        return _cleanup.prepare_cleanup()

    def cleanup(self, plan: DockerBottleCleanupPlan) -> None:
        _cleanup.cleanup(plan)

    def enumerate_active(self) -> Sequence[ActiveAgent]:
        return _enumerate.enumerate_active()
