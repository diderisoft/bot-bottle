"""SmolmachinesFreezer — snapshot a smolmachines bottle via smolvm pack."""

from __future__ import annotations

import sys

from .. import ActiveAgent
from ..freeze import CommitCancelled, Freezer
from .smolvm import machine_is_running, machine_stop, pack_create_from_vm
from ...bottle_state import bottle_state_dir
from ...log import info


def _read_tty_line() -> str:
    """Read one line from /dev/tty, falling back to stdin."""
    try:
        with open("/dev/tty", "r", encoding="utf-8") as tty:
            return tty.readline().rstrip("\n")
    except OSError:
        return sys.stdin.readline().rstrip("\n")


class SmolmachinesFreezer(Freezer):
    """Freezes a smolmachines bottle via `smolvm pack create --from-vm`.

    `smolvm pack create --from-vm` requires the VM to be stopped first.
    If the VM is running the freezer prompts the user to confirm the stop
    before proceeding. The VM remains stopped after commit; use
    `./cli.py resume` to restart."""

    backend_name = "smolmachines"

    def _freeze(self, agent: ActiveAgent) -> str:
        machine = f"bot-bottle-{agent.slug}"
        output = bottle_state_dir(agent.slug) / "committed-smolmachine"
        output.parent.mkdir(parents=True, exist_ok=True)
        if machine_is_running(machine):
            sys.stderr.write(
                f"bot-bottle: bottle {agent.slug!r} is running; "
                "commit will stop it. Continue? [y/N] "
            )
            sys.stderr.flush()
            reply = _read_tty_line().strip().lower()
            if reply not in ("y", "yes"):
                raise CommitCancelled
            machine_stop(machine)
        pack_create_from_vm(machine, output)
        artifact = output.with_name(f"{output.name}.smolmachine")
        return str(artifact)

    def _export_hint(self, slug: str, image_ref: str) -> None:
        info(f"to export for migration:      cp {image_ref} {slug}.smolmachine")
