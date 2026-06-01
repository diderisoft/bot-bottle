"""Provision non-secret provider auth markers into a Docker bottle."""

from __future__ import annotations

import os
import shlex
import subprocess

from ..bottle_plan import DockerBottlePlan


_DEFAULT_GUEST_HOME = "/home/node"


def provision_provider_auth(plan: DockerBottlePlan, target: str) -> None:
    """Prepare Codex home state inside a Docker bottle.

    Every Codex bottle gets a minimal config.toml that trusts the
    in-container launch directory. When host credentials are forwarded,
    auth.json contains no real access or refresh token values; it only
    nudges Codex into the same user/device auth branch as the host.
    """
    if plan.agent_provider_template != "codex":
        return
    container_home = os.environ.get(
        "BOT_BOTTLE_CONTAINER_HOME", _DEFAULT_GUEST_HOME,
    )
    auth_dir = f"{container_home}/.codex"

    subprocess.run(
        ["docker", "exec", "-u", "0", target, "mkdir", "-p", auth_dir],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        ["docker", "exec", "-u", "0", target, "chown", "node:node", auth_dir],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        ["docker", "exec", "-u", "0", target, "chmod", "700", auth_dir],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    config_path = f"{auth_dir}/config.toml"
    config = (
        f'[projects."{container_home}"]\n'
        'trust_level = "trusted"\n'
    )
    subprocess.run(
        [
            "docker", "exec", "-u", "0", target,
            "sh", "-c",
            f"printf %s {shlex.quote(config)} > {shlex.quote(config_path)}",
        ],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        ["docker", "exec", "-u", "0", target, "chown", "node:node", config_path],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        ["docker", "exec", "-u", "0", target, "chmod", "600", config_path],
        stdout=subprocess.DEVNULL,
        check=True,
    )

    if not plan.codex_auth_file:
        return

    auth_path = f"{auth_dir}/auth.json"
    subprocess.run(
        ["docker", "cp", str(plan.codex_auth_file), f"{target}:{auth_path}"],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        ["docker", "exec", "-u", "0", target, "chown", "node:node", auth_path],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        ["docker", "exec", "-u", "0", target, "chmod", "600", auth_path],
        stdout=subprocess.DEVNULL,
        check=True,
    )
