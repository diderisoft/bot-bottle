"""Cleanup for the Docker bottle backend.

PRD 0018 chunk 4: cleanup is centered on `docker compose ls`.
Pre-compose code paths could leave bare containers / networks
without a compose project; those still show up via the prefix
scan, just as a fallback bucket alongside the project list.

`prepare_cleanup` enumerates:

  - Live compose projects whose name starts with `claude-bottle-`.
  - `claude-bottle-*` containers that aren't part of any compose
    project (legacy orphans).
  - `claude-bottle-*` networks that aren't tied to a compose
    project (legacy orphans; compose-managed networks come down
    with `compose down --volumes` and don't appear here).
  - State dirs under ~/.claude-bottle/state/<identity>/ with no
    live compose project AND no `.preserve` marker.

`cleanup` removes everything in the plan.

Active-agent enumeration lives in `backend/docker/enumerate.py`
(mirror of `backend/smolmachines/enumerate.py`).
"""

from __future__ import annotations

import shutil
import subprocess

from ... import supervise as _supervise
from ...log import info, warn
from . import util as docker_mod
from .bottle_cleanup_plan import DockerBottleCleanupPlan
from .bottle_state import bottle_state_dir, is_preserved
from .compose import COMPOSE_PROJECT_PREFIX, list_compose_projects


def _list_prefixed_containers() -> list[str]:
    """All claude-bottle-prefixed containers, running or stopped."""
    result = subprocess.run(
        ["docker", "ps", "-a",
         "--filter", f"name=^{COMPOSE_PROJECT_PREFIX}",
         "--format", "{{.Names}}\t{{.Label \"com.docker.compose.project\"}}"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        warn(f"docker ps failed: {result.stderr.strip()}")
        return []
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
    """All claude-bottle-prefixed networks not currently attached
    to a compose project. Compose-managed networks have a
    `com.docker.compose.project` label; bare ones (from pre-compose
    code paths) don't."""
    result = subprocess.run(
        ["docker", "network", "ls",
         "--filter", f"name={COMPOSE_PROJECT_PREFIX}",
         "--format", "{{.Name}}\t{{.Label \"com.docker.compose.project\"}}"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        warn(f"docker network ls failed: {result.stderr.strip()}")
        return []
    out: list[str] = []
    for line in (result.stdout or "").splitlines():
        if not line:
            continue
        name, _, project = line.partition("\t")
        if not project:
            out.append(name)
    return sorted(set(out))


def _list_orphan_state_dirs(live_projects: set[str]) -> list[str]:
    """State identities whose compose project isn't running and
    that don't have a `.preserve` marker. `.preserve` means the
    user (or an auto-preserve-on-crash) wants the state kept for
    `resume`."""
    state_root = _supervise.claude_bottle_root() / "state"
    if not state_root.is_dir():
        return []
    orphans: list[str] = []
    for child in sorted(state_root.iterdir()):
        if not child.is_dir():
            continue
        identity = child.name
        project = f"{COMPOSE_PROJECT_PREFIX}{identity}"
        if project in live_projects:
            continue
        if is_preserved(identity):
            continue
        orphans.append(identity)
    return orphans


def prepare_cleanup() -> DockerBottleCleanupPlan:
    """Enumerate everything cleanup will touch. No removals."""
    docker_mod.require_docker()
    projects = list_compose_projects()
    project_set = set(projects)
    return DockerBottleCleanupPlan(
        projects=tuple(projects),
        stray_containers=tuple(_list_prefixed_containers()),
        stray_networks=tuple(_list_prefixed_networks()),
        orphan_state_dirs=tuple(_list_orphan_state_dirs(project_set)),
    )


def cleanup(plan: DockerBottleCleanupPlan) -> None:
    """Remove everything in the plan. Projects first (whose `compose
    down` reaps their containers + networks atomically), then stray
    legacy resources, then orphan state dirs."""
    for project in plan.projects:
        info(f"docker compose down  ({project})")
        result = subprocess.run(
            ["docker", "compose", "-p", project, "down", "--volumes"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            warn(
                f"compose down failed for {project}: "
                f"{result.stderr.strip()}"
            )

    for name in plan.stray_containers:
        info(f"removing stray container {name}")
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    for name in plan.stray_networks:
        info(f"removing stray network {name}")
        subprocess.run(
            ["docker", "network", "rm", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    for identity in plan.orphan_state_dirs:
        path = bottle_state_dir(identity)
        info(f"removing orphan state dir {path}")
        try:
            shutil.rmtree(path, ignore_errors=True)
        except OSError as e:
            warn(f"failed to remove {path}: {e}")


# `enumerate_active` moved to `backend/docker/enumerate.py` to
# mirror the smolmachines layout. Cleanup keeps the orphan
# enumeration; enumeration of live agents is its own concern.
