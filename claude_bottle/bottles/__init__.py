"""Per-platform bottle factories.

A bottle is a running, isolated environment with claude inside. Each
platform exposes four methods:

  prepare(spec, stage_dir=...) -> BottlePlan
      Resolves names, validates host-side prerequisites, and writes
      scratch files. No remote/runtime resources are created yet.
      Safe to call before the y/N preflight.

  launch(plan) -> ContextManager[Bottle]
      Brings up the container (or VM, or remote machine), provisions
      it, yields a Bottle handle, and tears everything down on exit.

  prepare_cleanup() -> BottleCleanupPlan
      Enumerates orphaned resources left behind by previous bottles
      (containers, networks, ...). Idempotent; no side effects.

  cleanup(plan) -> None
      Actually removes everything described by the cleanup plan.

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
from typing import Protocol

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


@dataclass(frozen=True)
class BottleCleanupPlan(ABC):
    """Base output of a platform's prepare_cleanup step. Concrete
    subclasses (e.g. DockerBottleCleanupPlan) carry platform-specific
    lists of resources to be removed and implement `print` + `empty`."""

    @abstractmethod
    def print(self) -> None:
        """Render the cleanup y/N summary to stderr."""

    @property
    @abstractmethod
    def empty(self) -> bool:
        """True iff there is nothing to clean up; the CLI uses this to
        short-circuit before showing the y/N."""


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


class BottlePlatform(ABC):
    """Abstract base for selectable bottle platforms. Concrete subclasses
    (e.g. DockerBottlePlatform) own their own prepare/launch impls.
    Symmetric with the BottlePlan → DockerBottlePlan hierarchy."""

    name: str

    @abstractmethod
    def prepare(self, spec: BottleSpec, *, stage_dir: Path) -> BottlePlan:
        """Resolve names, validate host-side prerequisites, write
        scratch files. No remote/runtime resources created yet."""

    @abstractmethod
    def launch(self, plan: BottlePlan) -> AbstractContextManager[Bottle]:
        """Build/run the bottle and yield a handle; tear down on exit."""

    @abstractmethod
    def prepare_cleanup(self) -> BottleCleanupPlan:
        """Enumerate orphaned resources from previous bottles. No side
        effects; safe to call before the y/N."""

    @abstractmethod
    def cleanup(self, plan: BottleCleanupPlan) -> None:
        """Remove everything described by the cleanup plan."""


# Import concrete platform classes AFTER the base types are defined, so
# each platform module can pull BottleSpec / BottlePlan / BottlePlatform
# via `from . import ...` without hitting a partially-initialized module.
from .docker import DockerBottlePlatform  # noqa: E402


_PLATFORMS: dict[str, BottlePlatform] = {
    "docker": DockerBottlePlatform(),
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
    "BottleCleanupPlan",
    "BottlePlan",
    "BottlePlatform",
    "BottleSpec",
    "get_bottle_platform",
]
