"""SmolmachinesFreezer — snapshot a smolmachines bottle via smolvm pack."""

from __future__ import annotations

from .. import ActiveAgent
from ..freeze import Freezer
from .smolvm import pack_create_from_vm
from ...bottle_state import bottle_state_dir
from ...log import info


class SmolmachinesFreezer(Freezer):
    """Freezes a smolmachines bottle via `smolvm pack create --from-vm`."""

    backend_name = "smolmachines"

    def _freeze(self, agent: ActiveAgent) -> str:
        machine = f"bot-bottle-{agent.slug}"
        output = bottle_state_dir(agent.slug) / "committed-smolmachine"
        output.parent.mkdir(parents=True, exist_ok=True)
        pack_create_from_vm(machine, output)
        artifact = output.with_name(f"{output.name}.smolmachine")
        return str(artifact)

    def _export_hint(self, slug: str, image_ref: str) -> None:
        info(f"to export for migration:      cp {image_ref} {slug}.smolmachine")
