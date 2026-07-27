"""Cleanup for the macOS Apple Container backend."""

from __future__ import annotations

import subprocess

from .. import EnumerationError
from ...log import info
from . import util as container_mod
from .bottle_cleanup_plan import MacosContainerBottleCleanupPlan

_PREFIX = "bot-bottle-"


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
        if name.startswith(_PREFIX)
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
        if name.startswith(_PREFIX)
    )


def prepare_cleanup() -> MacosContainerBottleCleanupPlan:
    container_mod.require_container()
    return MacosContainerBottleCleanupPlan(
        containers=tuple(_list_prefixed_containers()),
        networks=tuple(_list_prefixed_networks()),
    )


def cleanup(plan: MacosContainerBottleCleanupPlan) -> None:
    for name in plan.containers:
        info(f"container delete --force {name}")
        subprocess.run(
            ["container", "delete", "--force", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    for name in plan.networks:
        info(f"container network delete {name}")
        subprocess.run(
            ["container", "network", "delete", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
