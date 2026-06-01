"""Provision non-secret provider auth markers into a Docker bottle."""

from __future__ import annotations

import subprocess

from ..bottle_plan import DockerBottlePlan


def provision_provider_auth(plan: DockerBottlePlan, target: str) -> None:
    """Apply provider-owned guest setup through Docker primitives."""
    provision = plan.agent_provision
    for d in provision.dirs:
        _exec(target, ["mkdir", "-p", d.guest_path])
        _exec(target, ["chown", d.owner, d.guest_path])
        _exec(target, ["chmod", d.mode, d.guest_path])
    for command in provision.pre_copy:
        _exec(target, list(command.argv))
    for f in provision.files:
        subprocess.run(
            ["docker", "cp", str(f.host_path), f"{target}:{f.guest_path}"],
            stdout=subprocess.DEVNULL,
            check=True,
        )
        _exec(target, ["chown", f.owner, f.guest_path])
        _exec(target, ["chmod", f.mode, f.guest_path])
    for command in provision.verify:
        _exec(target, list(command.argv))


def _exec(target: str, argv: list[str]) -> None:
    subprocess.run(
        ["docker", "exec", "-u", "0", target, *argv],
        stdout=subprocess.DEVNULL,
        check=True,
    )
