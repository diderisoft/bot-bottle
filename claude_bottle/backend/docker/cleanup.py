"""Cleanup + active-listing for the Docker bottle backend.

`prepare_cleanup` enumerates orphaned `claude-bottle-` containers and
networks; `cleanup` removes them. `list_active` queries the same
namespace for ad-hoc inspection. All three share a single concern:
acting on resources whose names start with `claude-bottle-`.
"""

from __future__ import annotations

import subprocess

from ...log import info
from . import util as docker_mod
from .bottle_cleanup_plan import DockerBottleCleanupPlan


def prepare_cleanup() -> DockerBottleCleanupPlan:
    """Enumerate all claude-bottle-prefixed containers (running or
    stopped) and networks. No removals — caller confirms first."""
    docker_mod.require_docker()

    # `docker ps -a --filter name=...` uses regex matching; anchor at
    # the start so we don't pick up containers that merely contain
    # "claude-bottle-" mid-name.
    cr = subprocess.run(
        [
            "docker", "ps", "-a",
            "--filter", "name=^claude-bottle-",
            "--format", "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    containers = tuple(sorted(
        line for line in (cr.stdout or "").splitlines() if line
    ))

    # `docker network ls --filter name=...` uses substring matching.
    # "claude-bottle-" is specific enough that false positives are
    # not a concern.
    nr = subprocess.run(
        [
            "docker", "network", "ls",
            "--filter", "name=claude-bottle-",
            "--format", "{{.Name}}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    networks = tuple(sorted(
        line for line in (nr.stdout or "").splitlines() if line
    ))

    return DockerBottleCleanupPlan(containers=containers, networks=networks)


def cleanup(plan: DockerBottleCleanupPlan) -> None:
    """Remove the containers and networks listed in the plan.
    Containers first; networks would refuse to delete while containers
    are still attached."""
    for name in plan.containers:
        info(f"removing container {name}")
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    for name in plan.networks:
        info(f"removing network {name}")
        subprocess.run(
            ["docker", "network", "rm", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def list_active() -> None:
    """Print all running claude-bottle containers (name + status).
    Prints a single-line banner if there are none."""
    docker_mod.require_docker()
    result = subprocess.run(
        [
            "docker", "ps",
            "--filter", "name=^claude-bottle-",
            "--format", "{{.Names}}\t{{.Status}}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    containers = (result.stdout or "").strip()
    if not containers:
        info("no active claude-bottle containers")
        return
    print()
    for line in containers.splitlines():
        name, _, status = line.partition("\t")
        info(f"container: {name}  status: {status}")
    print()
