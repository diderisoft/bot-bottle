"""SmolmachinesBottleBackend — the smolmachines implementation of
BottleBackend (PRD 0023)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Sequence

from .. import ActiveAgent, BottleBackend, BottleSpec
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
from .provision import prompt as _prompt
from .provision import skills as _skills
from .provision import supervise as _supervise


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
        self, plan: SmolmachinesBottlePlan, target: str
    ) -> None:
        _ca.provision_ca(plan, target)

    def provision_prompt(
        self, plan: SmolmachinesBottlePlan, target: str
    ) -> str | None:
        return _prompt.provision_prompt(plan, target)

    def provision_skills(
        self, plan: SmolmachinesBottlePlan, target: str
    ) -> None:
        _skills.provision_skills(plan, target)

    def provision_git(
        self, plan: SmolmachinesBottlePlan, target: str
    ) -> None:
        _git.provision_git(plan, target)

    def provision_supervise(
        self, plan: SmolmachinesBottlePlan, target: str
    ) -> None:
        _supervise.provision_supervise(plan, target)

    def prepare_cleanup(self) -> SmolmachinesBottleCleanupPlan:
        return _cleanup.prepare_cleanup()

    def cleanup(self, plan: SmolmachinesBottleCleanupPlan) -> None:
        _cleanup.cleanup(plan)

    def enumerate_active(self) -> Sequence[ActiveAgent]:
        return _enumerate.enumerate_active()
