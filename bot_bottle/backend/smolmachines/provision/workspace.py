"""Copy the operator workspace into a smolmachines guest."""

from __future__ import annotations

import shlex

from ....log import info
from .. import smolvm as _smolvm
from ..bottle_plan import SmolmachinesBottlePlan


def provision_workspace(plan: SmolmachinesBottlePlan, target: str) -> None:
    """Copy host cwd contents to the planned guest workspace."""
    workspace = plan.workspace_plan
    if not (workspace.enabled and workspace.copy_contents):
        return

    guest_parent = workspace.guest_path.rsplit("/", 1)[0] or "/"
    guest_path_q = shlex.quote(workspace.guest_path)
    guest_parent_q = shlex.quote(guest_parent)
    owner_q = shlex.quote(workspace.owner)
    mode_q = shlex.quote(workspace.mode)
    info(f"copying {workspace.host_path} -> {target}:{workspace.guest_path}")
    _smolvm.machine_exec(
        target,
        ["sh", "-c", f"rm -rf {guest_path_q} && mkdir -p {guest_parent_q}"],
    )
    _smolvm.machine_cp(str(workspace.host_path), f"{target}:{workspace.guest_path}")
    _smolvm.machine_exec(
        target,
        [
            "sh", "-c",
            f"chown -R {owner_q} {guest_path_q} && "
            f"chmod {mode_q} {guest_path_q}",
        ],
    )
