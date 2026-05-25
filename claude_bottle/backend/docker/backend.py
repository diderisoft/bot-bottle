"""DockerBottleBackend — the Docker implementation of BottleBackend.

This module is a thin façade. The real work lives in three siblings:

  - prepare.py  — host-side resolution into a DockerBottlePlan
  - launch.py   — bring-up + teardown context manager
  - cleanup.py  — orphan enumeration, removal, and active listing

The base class's `prepare` template runs cross-backend host-side
validation before calling `_resolve_plan` here.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from .. import BottleBackend, BottleSpec
from . import cleanup as _cleanup
from . import launch as _launch
from . import prepare as _prepare
from .bottle import DockerBottle
from .bottle_cleanup_plan import DockerBottleCleanupPlan
from .bottle_plan import DockerBottlePlan
from .egress_proxy import DockerEgressProxy
from .git_gate import DockerGitGate
from .pipelock import DockerPipelockProxy
from .provision import ca as _ca
from .provision import git as _git
from .provision import prompt as _prompt
from .provision import skills as _skills
from .provision import supervise as _supervise_prov
from .supervise import DockerSupervise


class DockerBottleBackend(BottleBackend["DockerBottlePlan", "DockerBottleCleanupPlan"]):
    """Docker backend implementation. Selected by CLAUDE_BOTTLE_BACKEND
    (default)."""

    name = "docker"

    def __init__(self) -> None:
        self._proxy = DockerPipelockProxy()
        self._git_gate = DockerGitGate()
        self._egress_proxy = DockerEgressProxy()
        self._supervise = DockerSupervise()

    def _resolve_plan(self, spec: BottleSpec, *, stage_dir: Path) -> DockerBottlePlan:
        return _prepare.resolve_plan(
            spec,
            stage_dir=stage_dir,
            proxy=self._proxy,
            git_gate=self._git_gate,
            egress_proxy=self._egress_proxy,
            supervise=self._supervise,
        )

    @contextmanager
    def launch(self, plan: DockerBottlePlan) -> Generator[DockerBottle, None, None]:
        with _launch.launch(
            plan,
            proxy=self._proxy,
            git_gate=self._git_gate,
            egress_proxy=self._egress_proxy,
            supervise=self._supervise,
            provision=self.provision,
        ) as bottle:
            yield bottle

    def provision_ca(self, plan: DockerBottlePlan, target: str) -> None:
        _ca.provision_ca(plan, target)

    def provision_prompt(self, plan: DockerBottlePlan, target: str) -> str | None:
        return _prompt.provision_prompt(plan, target)

    def provision_skills(self, plan: DockerBottlePlan, target: str) -> None:
        _skills.provision_skills(plan, target)

    def provision_git(self, plan: DockerBottlePlan, target: str) -> None:
        _git.provision_git(plan, target)

    def provision_supervise(self, plan: DockerBottlePlan, target: str) -> None:
        _supervise_prov.provision_supervise(plan, target)

    def prepare_cleanup(self) -> DockerBottleCleanupPlan:
        return _cleanup.prepare_cleanup()

    def cleanup(self, plan: DockerBottleCleanupPlan) -> None:
        _cleanup.cleanup(plan)

    def list_active(self) -> None:
        _cleanup.list_active()
