"""Per-platform bottle factories.

A bottle is a running, isolated environment with claude inside. Each
platform exposes two functions:

  prepare(spec, stage_dir=...) -> BottlePlan
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
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from ..log import die
from ..manifest import Manifest


@dataclass(frozen=True)
class BottleSpec:
    """CLI-supplied intent. Platform-agnostic — each platform's prepare
    step consumes it and produces its own platform-specific plan.
    Resolved values (image names, container name, scratch paths, runsc
    availability) live on the plan, not the spec."""

    manifest: Manifest
    agent_name: str
    copy_cwd: bool
    user_cwd: str
    forward_oauth_token: bool


@dataclass(frozen=True)
class BottlePlan(ABC):
    """Base output of a platform's prepare step. Concrete subclasses
    (e.g. DockerBottlePlan) add platform-specific resolved fields and
    implement `print`."""

    spec: BottleSpec
    stage_dir: Path

    @abstractmethod
    def print(self, *, remote_control: bool) -> None:
        """Render the y/N preflight summary to stderr."""


# Import concrete platform factories AFTER the base types are defined,
# so each platform module can pull BottleSpec / BottlePlan via
# `from . import ...` without hitting a partially-initialized module.
from .docker import launch_docker_bottle, prepare_docker_bottle  # noqa: E402


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
    prepare: Callable[..., BottlePlan]
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


__all__ = [
    "Bottle",
    "BottlePlan",
    "BottlePlatform",
    "BottleSpec",
    "get_bottle_platform",
]
