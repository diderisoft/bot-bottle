"""Cleanup for the macOS Apple Container backend."""

from __future__ import annotations

import subprocess

from ...environment import resource_prefix
from .. import EnumerationError
from ...contract.hostctl import Settings
from ..cleanup_control import CleanupFailures, kept_guests, orphan_homes
from ...lib.log import info
from . import util as container_mod
from .bottle_cleanup_plan import MacosContainerBottleCleanupPlan
from .infra import INFRA_CONTAINERS, INFRA_NETWORKS

_PREFIX = resource_prefix()


def _list_prefixed_containers() -> list[str]:
    result = subprocess.run(
        ["container", "list", "--all", "--quiet"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise EnumerationError(f"container list failed: {detail}")
    return sorted(
        name for name in (line.strip() for line in result.stdout.splitlines())
        if name.startswith(_PREFIX) and name not in INFRA_CONTAINERS
    )


def _list_prefixed_networks() -> list[str]:
    result = subprocess.run(
        ["container", "network", "list", "--quiet"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise EnumerationError(f"container network list failed: {detail}")
    return sorted(
        name for name in (line.strip() for line in result.stdout.splitlines())
        if name.startswith(_PREFIX) and name not in INFRA_NETWORKS
    )


def prepare_cleanup() -> MacosContainerBottleCleanupPlan:
    container_mod.require_container()
    containers = _list_prefixed_containers()
    kept = kept_guests(containers)
    return MacosContainerBottleCleanupPlan(
        containers=tuple(c for c in containers if c not in kept),
        networks=tuple(_list_prefixed_networks()),
        volumes=tuple(orphan_homes(container_mod.list_volumes())),
    )


def cleanup(plan: MacosContainerBottleCleanupPlan, settings: Settings) -> None:
    failures = CleanupFailures(timeout=settings.operation.cleanup_command_timeout_seconds)
    for name in plan.containers:
        info(f"container delete --force {name}")
        failures.run(
            ["container", "delete", "--force", name],
            f"deleting container {name}",
        )
    for name in plan.networks:
        info(f"container network delete {name}")
        failures.run(
            ["container", "network", "delete", name],
            f"deleting network {name}",
        )
    for name in plan.volumes:
        info(f"container volume delete {name}")
        failures.run(
            ["container", "volume", "delete", name],
            f"deleting home volume {name}",
        )
    failures.raise_if_any()
