"""DockerBottleBackend — the Docker implementation of BottleBackend.

This module is a thin façade. The real work lives in four siblings:

  - prepare.py    — host-side resolution into a DockerBottlePlan
  - launch.py     — bring-up + teardown context manager
  - cleanup.py    — orphan enumeration + removal
  - enumerate.py  — active-agent listing

The base class's `prepare` template runs cross-backend host-side
validation before calling `_resolve_plan` here.
"""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Sequence

from .. import ActiveAgent, BottleBackend, BottleSpec
from . import cleanup as _cleanup
from . import enumerate as _enumerate
from . import launch as _launch
from . import prepare as _prepare
from .bottle import DockerBottle
from .bottle_cleanup_plan import DockerBottleCleanupPlan
from .bottle_plan import DockerBottlePlan
from .provision import ca as _ca
from .provision import git as _git
from .provision import prompt as _prompt
from .provision import provider_auth as _provider_auth
from .provision import skills as _skills
from .provision import supervise as _supervise_prov


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

    def provision_ca(self, plan: DockerBottlePlan, target: str) -> None:
        _ca.provision_ca(plan, target)

    def provision_prompt(self, plan: DockerBottlePlan, target: str) -> str | None:
        return _prompt.provision_prompt(plan, target)

    def provision_provider_auth(self, plan: DockerBottlePlan, target: str) -> None:
        _provider_auth.provision_provider_auth(plan, target)

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

    def enumerate_active(self) -> Sequence[ActiveAgent]:
        return _enumerate.enumerate_active()
