"""Provision non-secret provider auth markers into a smolmachines bottle."""

from __future__ import annotations

import shlex

from ....log import die
from ... import Bottle
from ..bottle_plan import SmolmachinesBottlePlan


def provision_provider_auth(plan: SmolmachinesBottlePlan, bottle: Bottle) -> None:
    """Apply provider-owned guest setup through the bottle's exec / cp_in."""
    provision = plan.agent_provision
    for d in provision.dirs:
        _exec(bottle, f"mkdir -p {shlex.quote(d.guest_path)}", f"could not create {d.guest_path}")
        _exec(bottle, f"chown {shlex.quote(d.owner)} {shlex.quote(d.guest_path)}", f"could not chown {d.guest_path}")
        _exec(bottle, f"chmod {shlex.quote(d.mode)} {shlex.quote(d.guest_path)}", f"could not chmod {d.guest_path}")
    for command in provision.pre_copy:
        _exec(bottle, shlex.join(command.argv), command.error)
    for f in provision.files:
        bottle.cp_in(str(f.host_path), f.guest_path)
        _exec(bottle, f"chown {shlex.quote(f.owner)} {shlex.quote(f.guest_path)}", f"could not chown {f.guest_path}")
        _exec(bottle, f"chmod {shlex.quote(f.mode)} {shlex.quote(f.guest_path)}", f"could not chmod {f.guest_path}")
    for command in provision.verify:
        _exec(bottle, shlex.join(command.argv), command.error)


def _exec(bottle: Bottle, script: str, error: str) -> None:
    result = bottle.exec(script, user="root")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            detail = f": {detail}"
        die(f"agent provider provisioning: {error}{detail}")
