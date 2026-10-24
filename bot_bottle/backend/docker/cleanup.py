"""Cleanup for the Docker bottle backend.

Cleanup is centered on `docker compose ls`.
Pre-compose code paths could leave bare containers / networks
without a compose project; those still show up via the prefix
scan, just as a fallback bucket alongside the project list.

`prepare_cleanup` enumerates:

  - Live compose projects whose name starts with `bot-bottle-`.
  - `bot-bottle-*` containers that aren't part of any compose
    project (legacy orphans).
  - `bot-bottle-*` networks that aren't tied to a compose
    project (legacy orphans; compose-managed networks come down
    with `compose down --volumes` and don't appear here).

State dirs are shared by every backend and reaped in
`backend/state_cleanup.py`.

`cleanup` removes everything in the plan.

Active-agent enumeration lives in `backend/docker/enumerate.py`.
"""

from __future__ import annotations

import subprocess

from ...contract.hostctl import Settings
from ...lib.log import info
from .. import EnumerationError
from ..cleanup_control import CleanupFailures
from . import util as docker_mod
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
        # Stray = no compose label. Compose-managed containers carry
        # `com.docker.compose.project=<name>`; we'll reap those via
        # `compose down`, not via container rm.
        if not project:
            out.append(name)
    return sorted(set(out))


def _list_prefixed_networks() -> list[str]:
    """All bot-bottle-prefixed networks not currently attached
    to a compose project. Compose-managed networks have a
    `com.docker.compose.project` label; bare ones (from pre-compose
    code paths) don't."""
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
    """Enumerate everything cleanup will touch. No removals. Raises
    EnumerationError when the Docker daemon can't be asked."""
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
    )


def cleanup(plan: DockerBottleCleanupPlan, settings: Settings) -> None:
    """Remove everything in the plan. Projects first (whose `compose
    down` reaps their containers + networks atomically), then stray
    legacy resources."""
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
        failures.run(
            ["docker", "network", "rm", name],
            f"removing stray network {name}",
        )
    failures.raise_if_any()
