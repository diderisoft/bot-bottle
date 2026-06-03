"""Copy the operator workspace into a smolmachines guest."""

from __future__ import annotations

import shlex

from ....log import info
from ... import Bottle
from ..bottle_plan import SmolmachinesBottlePlan


def provision_workspace(plan: SmolmachinesBottlePlan, bottle: Bottle) -> None:
    """Copy host cwd contents to the planned guest workspace."""
    workspace = plan.workspace_plan
    if not (workspace.enabled and workspace.copy_contents):
        return

    guest_parent = workspace.guest_path.rsplit("/", 1)[0] or "/"
    guest_path_q = shlex.quote(workspace.guest_path)
    guest_parent_q = shlex.quote(guest_parent)
    owner_q = shlex.quote(workspace.owner)
    mode_q = shlex.quote(workspace.mode)
    info(f"copying {workspace.host_path} -> {bottle.name}:{workspace.guest_path}")
    bottle.exec(
        f"rm -rf {guest_path_q} && mkdir -p {guest_parent_q}",
        user="root",
    )
    bottle.cp_in(str(workspace.host_path), workspace.guest_path)
    bottle.exec(
        f"chown -R {owner_q} {guest_path_q} && chmod {mode_q} {guest_path_q}",
        user="root",
    )
