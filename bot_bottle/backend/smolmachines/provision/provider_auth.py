"""Provision non-secret provider auth markers into a smolmachines bottle."""

from __future__ import annotations

import os
import shlex

from ....log import die
from .. import smolvm as _smolvm
from ..bottle_plan import SmolmachinesBottlePlan


_DEFAULT_GUEST_HOME = "/home/node"
_CODEX_WORKSPACE = "/home/node/workspace"


def provision_provider_auth(plan: SmolmachinesBottlePlan, target: str) -> None:
    """Prepare Codex home state inside the smolmachine.

    Every Codex bottle gets a minimal config.toml that trusts the
    in-guest workspace path. When host credentials are forwarded, the
    real host access token remains in the egress bundle env; auth.json
    only selects Codex's user/device auth code path.
    """
    if plan.agent_provider_template != "codex":
        return
    guest_home = os.environ.get("BOT_BOTTLE_GUEST_HOME", _DEFAULT_GUEST_HOME)
    auth_dir = plan.guest_env.get("CODEX_HOME", f"{guest_home}/.codex")

    result = _smolvm.machine_exec(
        target,
        ["mkdir", "-p", auth_dir],
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            detail = f": {detail}"
        die(f"codex host credentials: could not create {auth_dir}{detail}")
    result = _smolvm.machine_exec(target, ["chown", "node:node", auth_dir])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            detail = f": {detail}"
        die(f"codex host credentials: could not chown {auth_dir}{detail}")
    result = _smolvm.machine_exec(target, ["chmod", "700", auth_dir])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            detail = f": {detail}"
        die(f"codex host credentials: could not chmod {auth_dir}{detail}")
    result = _smolvm.machine_exec(
        target,
        [
            "find", auth_dir,
            "-maxdepth", "1",
            "-type", "f",
            "(",
            "-name", "*.sqlite",
            "-o", "-name", "*.sqlite-*",
            "-o", "-name", "*.codex-repair-*.bak",
            ")",
            "-delete",
        ],
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            detail = f": {detail}"
        die(f"codex host credentials: could not reset runtime db files{detail}")

    config_path = f"{auth_dir}/config.toml"
    config = (
        f'[projects."{_CODEX_WORKSPACE}"]\n'
        'trust_level = "trusted"\n'
    )
    result = _smolvm.machine_exec(
        target,
        [
            "sh", "-c",
            f"printf %s {shlex.quote(config)} > {shlex.quote(config_path)}",
        ],
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            detail = f": {detail}"
        die(f"codex host credentials: could not write {config_path}{detail}")
    _smolvm.machine_exec(target, ["chown", "node:node", config_path])
    _smolvm.machine_exec(target, ["chmod", "600", config_path])

    if not plan.codex_auth_file:
        return

    auth_path = f"{auth_dir}/auth.json"
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
