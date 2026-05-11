"""Per-backend bottle factories.

A bottle is a running, isolated environment with claude inside. Each
backend exposes five methods:

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

  list_active() -> None
      Print every currently-running bottle on this backend to stderr.

Selection is driven by CLAUDE_BOTTLE_BACKEND (default "docker"). Per
PRD 0003 the manifest does not carry a backend field; the host
environment picks.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from ..log import die
from ..manifest import Manifest


@dataclass(frozen=True)
class BottleSpec:
    """CLI-supplied intent. Backend-agnostic — each backend's prepare
    step consumes it and produces its own backend-specific plan.
    Resolved values (image names, container name, scratch paths, runsc
    availability) live on the plan, not the spec."""

    manifest: Manifest
    agent_name: str
    copy_cwd: bool
    user_cwd: str
    forward_oauth_token: bool


@dataclass(frozen=True)
class BottlePlan(ABC):
    """Base output of a backend's prepare step. Concrete subclasses
    (e.g. DockerBottlePlan) add backend-specific resolved fields and
    implement `print`."""

    spec: BottleSpec
    stage_dir: Path

    @abstractmethod
    def print(self, *, remote_control: bool) -> None:
        """Render the y/N preflight summary to stderr."""


@dataclass(frozen=True)
class BottleCleanupPlan(ABC):
    """Base output of a backend's prepare_cleanup step. Concrete
    subclasses (e.g. DockerBottleCleanupPlan) carry backend-specific
    lists of resources to be removed and implement `print` + `empty`."""

    @abstractmethod
    def print(self) -> None:
        """Render the cleanup y/N summary to stderr."""

    @property
    @abstractmethod
    def empty(self) -> bool:
        """True iff there is nothing to clean up; the CLI uses this to
        short-circuit before showing the y/N."""


class Bottle(ABC):
    """Handle to a running bottle. Yielded by a backend's launch step.

    `exec_claude` runs `claude` inside the bottle and blocks until the
    session ends. `cp_in` copies a host path into the bottle. `close`
    is an idempotent alias for context-manager teardown.
    """

    name: str

    @abstractmethod
    def exec_claude(self, argv: list[str], *, tty: bool = True) -> int: ...

    @abstractmethod
    def cp_in(self, host_path: str, container_path: str) -> None: ...

    @abstractmethod
    def close(self) -> None: ...


class BottleProvisioner(ABC):
    """Copies host-side files (prompt, skills, SSH keys, .git) into a
    running bottle after the container/machine is up. Owned by a
    BottleBackend; called from its launch step before yielding the
    Bottle handle."""

    @abstractmethod
    def provision(self, plan: BottlePlan, target: str) -> str | None:
        """Provision the running bottle described by `plan`. `target`
        identifies the running instance in backend-specific terms
        (Docker: resolved container name; fly: machine id). Returns the
        in-container prompt path if a prompt was provisioned, else
        None — the Bottle handle uses it to decide whether to add
        --append-system-prompt-file to claude's argv."""


class BottleBackend(ABC):
    """Abstract base for selectable bottle backends. Concrete subclasses
    (e.g. DockerBottleBackend) own their own prepare/launch impls.
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

    @abstractmethod
    def list_active(self) -> None:
        """Print every currently-running bottle on this backend to
        stderr (name + status)."""


# Import concrete backend classes AFTER the base types are defined, so
# each backend module can pull BottleSpec / BottlePlan / BottleBackend
# via `from . import ...` without hitting a partially-initialized module.
from .docker import DockerBottleBackend  # noqa: E402


_BACKENDS: dict[str, BottleBackend] = {
    "docker": DockerBottleBackend(),
}


def get_bottle_backend() -> BottleBackend:
    """Resolve the bottle backend for the active environment. Dies with
    a pointer at the known backends if CLAUDE_BOTTLE_BACKEND names an
    unimplemented one."""
    name = os.environ.get("CLAUDE_BOTTLE_BACKEND", "docker")
    if name not in _BACKENDS:
        known = ", ".join(sorted(_BACKENDS))
        die(f"unknown CLAUDE_BOTTLE_BACKEND={name!r}; known backends: {known}")
    return _BACKENDS[name]


__all__ = [
    "Bottle",
    "BottleBackend",
    "BottleCleanupPlan",
    "BottlePlan",
    "BottleProvisioner",
    "BottleSpec",
    "get_bottle_backend",
]
