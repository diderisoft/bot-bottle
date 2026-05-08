"""Skill copier: host's ~/.claude/skills/<name>/ -> container's
~/.claude/skills/<name>/, preserving directory structure."""

from __future__ import annotations

import os
import subprocess

from .log import die, info

CONTAINER_HOME = os.environ.get("CLAUDE_BOTTLE_CONTAINER_HOME", "/home/node")
CONTAINER_SKILLS_DIR = os.environ.get(
    "CLAUDE_BOTTLE_CONTAINER_SKILLS_DIR", f"{CONTAINER_HOME}/.claude/skills"
)


def host_skill_dir(name: str) -> str:
    home = os.environ.get("HOME")
    if not home:
        die("HOME not set")
    return f"{home}/.claude/skills/{name}"


def host_skill_exists(name: str) -> bool:
    return os.path.isdir(host_skill_dir(name))


def require_host_skill(name: str) -> None:
    if not host_skill_exists(name):
        die(
            f"skill '{name}' not found on host at {host_skill_dir(name)}. "
            f"Create it under ~/.claude/skills/, then re-run."
        )


def skills_validate_all(names: list[str]) -> None:
    """Use BEFORE the y/N so the user does not get asked about a plan
    that's already known to fail."""
    for n in names:
        require_host_skill(n)


def skills_copy_into(container: str, names: list[str]) -> None:
    """For each named skill, ensure the parent dir exists, wipe any
    prior copy, then `docker cp <host>/. <container>:<dst>/` so the
    contents are copied into a freshly-created destination dir."""
    if not names:
        return

    subprocess.run(
        ["docker", "exec", container, "mkdir", "-p", CONTAINER_SKILLS_DIR],
        stdout=subprocess.DEVNULL,
        check=True,
    )

    for n in names:
        src = host_skill_dir(n)
        if not os.path.isdir(src):
            die(f"skill '{n}' disappeared from host between validation and copy at {src}.")
        dst = f"{CONTAINER_SKILLS_DIR}/{n}"
        info(f"copying skill {n} into {container}:{dst}")
        subprocess.run(
            ["docker", "exec", container, "rm", "-rf", dst],
            stdout=subprocess.DEVNULL,
            check=True,
        )
        subprocess.run(
            ["docker", "exec", container, "mkdir", "-p", dst],
            stdout=subprocess.DEVNULL,
            check=True,
        )
        subprocess.run(
            ["docker", "cp", f"{src}/.", f"{container}:{dst}/"],
            stdout=subprocess.DEVNULL,
            check=True,
        )
