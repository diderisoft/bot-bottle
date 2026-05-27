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
        with _launch.launch(plan) as bottle:
            yield bottle

    # The four `provision_*` methods land in chunk 4 alongside the
    # `smolvm machine cp`-based copy-in flow. Stubs raise so any
    # caller that reaches them before chunk 4 gets a clear pointer.
    def provision_prompt(
        self, plan: SmolmachinesBottlePlan, target: str
    ) -> str | None:
        raise NotImplementedError("smolmachines provision_prompt → chunk 4")

    def provision_skills(
        self, plan: SmolmachinesBottlePlan, target: str
    ) -> None:
        raise NotImplementedError("smolmachines provision_skills → chunk 4")

    def provision_git(
        self, plan: SmolmachinesBottlePlan, target: str
    ) -> None:
        raise NotImplementedError("smolmachines provision_git → chunk 4")

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
