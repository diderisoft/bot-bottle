"""SmolmachinesBottleBackend — the smolmachines implementation of
BottleBackend (PRD 0023)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from .. import BottleBackend, BottleSpec
from . import launch as _launch
from . import prepare as _prepare
from .bottle import SmolmachinesBottle
from .bottle_cleanup_plan import SmolmachinesBottleCleanupPlan
from .bottle_plan import SmolmachinesBottlePlan
from .provision import prompt as _prompt
from .provision import skills as _skills


class SmolmachinesBottleBackend(
    BottleBackend["SmolmachinesBottlePlan", "SmolmachinesBottleCleanupPlan"]
):
    """smolmachines backend. Selected by
    `CLAUDE_BOTTLE_BACKEND=smolmachines`."""

    name = "smolmachines"

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
        # Chunk 4 follow-on: needs the git-gate inner Plan (so the
        # gitconfig insteadOf URL points at the gate's host) and
        # the agent image must contain `git`. Stub for chunk 4a.
        del plan, target

    def prepare_cleanup(self) -> SmolmachinesBottleCleanupPlan:
        return SmolmachinesBottleCleanupPlan()

    def cleanup(self, plan: SmolmachinesBottleCleanupPlan) -> None:
        del plan
        # Nothing to clean in chunks 1-3 — see
        # SmolmachinesBottleCleanupPlan docstring.

    def list_active(self) -> None:
        from ...log import info
        info(
            "smolmachines list_active: not implemented (chunk 4 wires "
            "it to `smolvm machine ls --json`)"
        )
