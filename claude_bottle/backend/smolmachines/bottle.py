"""SmolmachinesBottle — runtime handle stub (PRD 0023 chunk 1).

The chunk-1 backend doesn't launch VMs yet, so this class only
exists to make `SmolmachinesBottleBackend.launch` resolvable at
import time. Every method raises NotImplementedError; chunk 2
gives it real `smolvm machine exec` plumbing."""

from __future__ import annotations

from .. import Bottle, ExecResult


class SmolmachinesBottle(Bottle):
    """Stub. Real impl lands in chunk 2."""

    def __init__(self, name: str) -> None:
        self.name = name

    def exec_claude(self, argv: list[str], *, tty: bool = True) -> int:
        del argv, tty
        raise NotImplementedError(
            "smolmachines backend chunk 1 ships prepare-only; "
            "exec_claude lands in chunk 2"
        )

    def exec(self, script: str) -> ExecResult:
        del script
        raise NotImplementedError(
            "smolmachines backend chunk 1 ships prepare-only; "
            "exec lands in chunk 2"
        )

    def cp_in(self, host_path: str, container_path: str) -> None:
        del host_path, container_path
        raise NotImplementedError(
            "smolmachines backend chunk 1 ships prepare-only; "
            "cp_in lands in chunk 2"
        )

    def close(self) -> None:
        pass
