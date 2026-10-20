"""Cleanup for the Docker bottle backend.

PRD 0018 chunk 4: cleanup is centered on `docker compose ls`.
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
  - State dirs under ~/.bot-bottle/state/<identity>/ with no
    live compose project AND no `.preserve` marker.

`cleanup` removes everything in the plan.

Active-agent enumeration lives in `backend/docker/enumerate.py`.
"""

from __future__ import annotations

import subprocess

from ...paths import bot_bottle_root
from ...lib.log import info
from .. import EnumerationError
from ..cleanup_control import CleanupFailures
from . import util as docker_mod
from .bottle_cleanup_plan import DockerBottleCleanupPlan
from ...bottle_state import bottle_state_dir, is_preserved
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


def _list_orphan_state_dirs(
    live_projects: set[str], protected_identities: set[str],
) -> list[str]:
    """State identities whose compose project isn't running and
    that don't have a `.preserve` marker. `.preserve` means the
    user (or an auto-preserve-on-crash) wants the state kept for
    `resume`.

    `protected_identities` is the set of slugs that are live in
    ANY backend — used so this docker-side check doesn't reap a
    running non-docker bottle's state dir (the layout is shared
    across backends)."""
    state_root = bot_bottle_root() / "state"
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
        if identity in protected_identities:
            continue
        if is_preserved(identity):
            continue
        orphans.append(identity)
    return orphans


def prepare_cleanup() -> DockerBottleCleanupPlan:
    """Enumerate everything cleanup will touch. No removals.

    Pulls the union of live identities across backends via
    `enumerate_active_agents()` so the orphan-state-dir bucket
    doesn't include slugs whose non-docker bottle is still up."""
    docker_mod.require_docker()
    projects = list_compose_projects(
        warn_on_error=False,
        raise_on_error=True,
    )
    project_set = set(projects)
    # Late import to avoid a circular at module-load time —
    # the backend package's __init__ imports this module.
    from .. import enumerate_active_agents
    protected = {a.slug for a in enumerate_active_agents()}
    return DockerBottleCleanupPlan(
        projects=tuple(projects),
        stray_containers=tuple(_list_prefixed_containers()),
        stray_networks=tuple(_list_prefixed_networks()),
        orphan_state_dirs=tuple(
            _list_orphan_state_dirs(project_set, protected),
        ),
    )


def cleanup(plan: DockerBottleCleanupPlan) -> None:
    """Remove everything in the plan. Projects first (whose `compose
    down` reaps their containers + networks atomically), then stray
    legacy resources, then orphan state dirs."""
    failures = CleanupFailures()
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

    for identity in plan.orphan_state_dirs:
        path = bottle_state_dir(identity)
        info(f"removing orphan state dir {path}")
        failures.remove_tree(path, f"removing orphan state dir {path}")
    failures.raise_if_any()
