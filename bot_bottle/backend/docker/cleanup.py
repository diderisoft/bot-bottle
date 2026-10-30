"""Cleanup for the docker backend: `bot-bottle-*` compose projects, plus
prefixed containers and networks outside any project. State dirs are reaped in
`backend/state_cleanup.py`.
"""

from __future__ import annotations

import subprocess

from ...contract.hostctl import Settings
from ...lib.log import info
from .. import EnumerationError
from ..cleanup_control import CleanupFailures, orphan_homes
from . import util as docker_mod
from ...control.gateway import GATEWAY_NAME
from .agent_network import is_agent_network
from .bottle_cleanup_plan import DockerBottleCleanupPlan
from .compose import COMPOSE_PROJECT_PREFIX, list_compose_projects


def _list_prefixed_containers() -> list[str]:
    """All bot-bottle-prefixed containers, running or stopped."""
    try:
        result = subprocess.run(
            ["docker", "ps", "-a",
             "--filter", f"name=^{COMPOSE_PROJECT_PREFIX}",
             "--format", "{{.Names}}\t{{.Label \"com.docker.compose.project\"}}"],
            capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        raise EnumerationError(f"docker ps failed: {exc}") from exc
    if result.returncode != 0:
        raise EnumerationError(f"docker ps failed: {result.stderr.strip()}")
    out: list[str] = []
    for line in (result.stdout or "").splitlines():
        if not line:
            continue
        name, _, project = line.partition("\t")
        # No compose label: not reaped by `compose down`.
        if not project:
            out.append(name)
    return sorted(set(out))


def _list_prefixed_networks() -> list[str]:
    """bot-bottle-prefixed networks without a compose project label."""
    try:
        result = subprocess.run(
            ["docker", "network", "ls",
             "--filter", f"name={COMPOSE_PROJECT_PREFIX}",
             "--format", "{{.Name}}\t{{.Label \"com.docker.compose.project\"}}"],
            capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        raise EnumerationError(f"docker network ls failed: {exc}") from exc
    if result.returncode != 0:
        raise EnumerationError(
            f"docker network ls failed: {result.stderr.strip()}"
        )
    out: list[str] = []
    for line in (result.stdout or "").splitlines():
        if not line:
            continue
        name, _, project = line.partition("\t")
        if not project:
            out.append(name)
    return sorted(set(out))


def prepare_cleanup(settings: Settings) -> DockerBottleCleanupPlan:
    """Everything cleanup will touch; removes nothing. Raises EnumerationError
    when the daemon can't be asked."""
    del settings
    docker_mod.require_docker()
    projects = list_compose_projects(
        warn_on_error=False,
        raise_on_error=True,
    )
    return DockerBottleCleanupPlan(
        projects=tuple(projects),
        stray_containers=tuple(_list_prefixed_containers()),
        stray_networks=tuple(_list_prefixed_networks()),
        volumes=tuple(orphan_homes(docker_mod.list_volumes())),
    )


def cleanup(plan: DockerBottleCleanupPlan, settings: Settings) -> None:
    """Remove everything in the plan: projects first, then stray resources."""
    failures = CleanupFailures(timeout=settings.operation.cleanup_command_timeout_seconds)
    for project in plan.projects:
        info(f"docker compose down  ({project})")
        failures.run(
            ["docker", "compose", "-p", project, "down", "--volumes"],
            f"docker compose down failed for {project}",
        )

    for name in plan.stray_containers:
        info(f"removing stray container {name}")
        failures.run(
            ["docker", "rm", "-f", name],
            f"removing stray container {name}",
        )

    for name in plan.stray_networks:
        info(f"removing stray network {name}")
        if is_agent_network(name):
            # Docker won't remove a network the gateway is still on. Best
            # effort: a failure shows up as the `network rm` failing.
            try:
                docker_mod.run_docker(
                    ["docker", "network", "disconnect", "--force", name, GATEWAY_NAME],
                    timeout=max(settings.operation.cleanup_command_timeout_seconds, 1.0))
            except (OSError, subprocess.SubprocessError):
                pass
        failures.run(
            ["docker", "network", "rm", name],
            f"removing stray network {name}",
        )
    # After the projects, which were the volumes' only users.
    for name in plan.volumes:
        info(f"docker volume rm {name}")
        failures.run(["docker", "volume", "rm", name], f"removing home volume {name}")
    failures.raise_if_any()
