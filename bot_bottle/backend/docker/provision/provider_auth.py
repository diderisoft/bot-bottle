"""Provision non-secret provider auth markers into a Docker bottle."""

from __future__ import annotations

import os
import subprocess

from ..bottle_plan import DockerBottlePlan


def provision_provider_auth(plan: DockerBottlePlan, target: str) -> None:
    """Copy a dummy Codex auth marker when host credentials are
    forwarded through egress.

    The file contains no real access or refresh token values; it only
    nudges Codex into the same user/device auth branch as the host.
    """
    if not plan.codex_auth_file:
        return
    container_home = os.environ.get("BOT_BOTTLE_CONTAINER_HOME", "/home/node")
    auth_dir = f"{container_home}/.codex"
    auth_path = f"{auth_dir}/auth.json"

    subprocess.run(
        ["docker", "exec", "-u", "0", target, "mkdir", "-p", auth_dir],
        stdout=subprocess.DEVNULL,
        check=True,
    )
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
