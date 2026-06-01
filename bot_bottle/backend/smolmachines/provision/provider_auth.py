"""Provision non-secret provider auth markers into a smolmachines bottle."""

from __future__ import annotations

from ....log import die
from .. import smolvm as _smolvm
from ..bottle_plan import SmolmachinesBottlePlan


def provision_provider_auth(plan: SmolmachinesBottlePlan, target: str) -> None:
    """Apply provider-owned guest setup through smolvm primitives."""
    provision = plan.agent_provision
    for d in provision.dirs:
        _exec(target, ["mkdir", "-p", d.guest_path], f"could not create {d.guest_path}")
        _exec(target, ["chown", d.owner, d.guest_path], f"could not chown {d.guest_path}")
        _exec(target, ["chmod", d.mode, d.guest_path], f"could not chmod {d.guest_path}")
    for command in provision.pre_copy:
        _exec(target, list(command.argv), command.error)
    for f in provision.files:
        _smolvm.machine_cp(str(f.host_path), f"{target}:{f.guest_path}")
        _exec(target, ["chown", f.owner, f.guest_path], f"could not chown {f.guest_path}")
        _exec(target, ["chmod", f.mode, f.guest_path], f"could not chmod {f.guest_path}")
    for command in provision.verify:
        _exec(target, list(command.argv), command.error)


def _exec(target: str, argv: list[str], error: str) -> None:
    result = _smolvm.machine_exec(target, argv)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            detail = f": {detail}"
        die(f"agent provider provisioning: {error}{detail}")
