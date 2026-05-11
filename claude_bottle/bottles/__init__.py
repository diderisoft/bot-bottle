"""Per-platform bottle factories.

A bottle is a running, isolated environment with claude inside. Each
platform exposes two functions:

  prepare(spec, stage_dir=...) -> Plan
      Resolves names, validates host-side prerequisites, and writes
      scratch files. No remote/runtime resources are created yet.
      Safe to call before the y/N preflight.

  launch(plan) -> ContextManager[Bottle]
      Brings up the container (or VM, or remote machine), provisions
      it, yields a Bottle handle, and tears everything down on exit.

Selection is driven by CLAUDE_BOTTLE_PLATFORM (default "docker"). Per
PRD 0003 the manifest does not carry a platform field; the host
environment picks.
"""

from __future__ import annotations

import os
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Callable, Protocol

from ..log import die
from .docker import launch_docker_bottle, prepare_docker_bottle


class Bottle(Protocol):
    """Handle to a running bottle. Yielded by a platform's launch step.

    `exec_claude` runs `claude` inside the bottle and blocks until the
    session ends. `cp_in` copies a host path into the bottle. `close`
    is an idempotent alias for context-manager teardown.
    """

    name: str

    def exec_claude(self, argv: list[str], *, tty: bool = True) -> int: ...
    def cp_in(self, host_path: str, container_path: str) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class BottlePlatform:
    """Bundles a platform's two-phase factory under one selectable name."""

    name: str
    prepare: Callable[..., object]
    launch: Callable[..., AbstractContextManager[Bottle]]


_PLATFORMS: dict[str, BottlePlatform] = {
    "docker": BottlePlatform(
        name="docker",
        prepare=prepare_docker_bottle,
        launch=launch_docker_bottle,
    ),
}


def get_bottle_platform() -> BottlePlatform:
    """Resolve the bottle platform for the active environment. Dies with
    a pointer at the known platforms if CLAUDE_BOTTLE_PLATFORM names an
    unimplemented one."""
    name = os.environ.get("CLAUDE_BOTTLE_PLATFORM", "docker")
    if name not in _PLATFORMS:
        known = ", ".join(sorted(_PLATFORMS))
        die(f"unknown CLAUDE_BOTTLE_PLATFORM={name!r}; known platforms: {known}")
    return _PLATFORMS[name]
