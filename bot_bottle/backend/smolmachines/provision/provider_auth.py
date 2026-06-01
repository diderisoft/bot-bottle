"""Provision non-secret provider auth markers into a smolmachines bottle."""

from __future__ import annotations

import os

from ....log import die
from .. import smolvm as _smolvm
from ..bottle_plan import SmolmachinesBottlePlan


_DEFAULT_GUEST_HOME = "/home/node"


def provision_provider_auth(plan: SmolmachinesBottlePlan, target: str) -> None:
    """Copy a dummy Codex auth marker when host credentials are
    forwarded through egress.

    The real host access token remains in the egress bundle env; this
    file only selects Codex's user/device auth code path.
    """
    if not plan.codex_auth_file:
        return
    guest_home = os.environ.get("BOT_BOTTLE_GUEST_HOME", _DEFAULT_GUEST_HOME)
    auth_dir = f"{guest_home}/.codex"
    auth_path = f"{auth_dir}/auth.json"

    _smolvm.machine_exec(target, ["mkdir", "-p", auth_dir])
    _smolvm.machine_cp(str(plan.codex_auth_file), f"{target}:{auth_path}")
    _smolvm.machine_exec(target, ["chown", "node:node", auth_path])
    _smolvm.machine_exec(target, ["chmod", "600", auth_path])
    result = _smolvm.machine_exec(
        target,
        [
            "runuser", "-u", "node", "--",
            "env",
            f"HOME={guest_home}",
            f"CODEX_HOME={auth_dir}",
            "codex", "login", "status",
        ],
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            detail = f": {detail}"
        die(
            "codex host credentials: dummy auth was copied into the "
            f"smolmachine, but Codex did not accept it{detail}"
        )
