"""Skill discovery and host-side validation. The copy step itself
lives on the backend (e.g. DockerBottleBackend.provision_skills)."""

from __future__ import annotations

import os

from .log import die


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
