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
from typing import Any, Generic, TypeVar

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

    @abstractmethod
    def to_dict(self, *, remote_control: bool) -> dict[str, object]:
        """Return the plan as a JSON-serializable dict for machine
        consumption (used by `start --dry-run --format=json`). The key
        set is part of the CLI's user-facing contract — adding fields
        is fine, renaming or removing is a breaking change."""


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




PlanT = TypeVar("PlanT", bound=BottlePlan)
CleanupT = TypeVar("CleanupT", bound=BottleCleanupPlan)


class BottleBackend(ABC, Generic[PlanT, CleanupT]):
    """Abstract base for selectable bottle backends. Concrete subclasses
    (e.g. DockerBottleBackend) own their own prepare/launch impls.
    Parameterized over the backend's concrete plan + cleanup-plan types
    so subclass methods get the narrow type without isinstance
    boilerplate."""

    name: str

    @abstractmethod
    def prepare(self, spec: BottleSpec, *, stage_dir: Path) -> PlanT:
        """Resolve names, validate host-side prerequisites, write
        scratch files. No remote/runtime resources created yet."""

    @abstractmethod
    def launch(self, plan: PlanT) -> AbstractContextManager[Bottle]:
        """Build/run the bottle and yield a handle; tear down on exit."""

    def provision(self, plan: PlanT, target: str) -> str | None:
        """Copy host-side files (prompt, skills, SSH keys, .git) into
        the running bottle. Called from `launch` after the container/
        machine is up. `target` identifies the running instance in
        backend-specific terms (Docker: resolved container name; fly:
        machine id). Returns the in-container prompt path if a prompt
        was provisioned, else None — the Bottle handle uses it to
        decide whether to add --append-system-prompt-file to claude's
        argv.

        Default orchestration: prompt → skills → ssh → git. Subclasses
        typically don't override this; they implement the four
        sub-methods below."""
        prompt_path = self.provision_prompt(plan, target)
        self.provision_skills(plan, target)
        self.provision_ssh(plan, target)
        self.provision_git(plan, target)
        return prompt_path

    @abstractmethod
    def provision_prompt(self, plan: PlanT, target: str) -> str | None:
        """Copy the prompt file into the running bottle. Returns the
        in-container path iff the agent has a non-empty prompt;
        callers use the return value to decide whether to add
        --append-system-prompt-file to claude's argv."""

    @abstractmethod
    def provision_skills(self, plan: PlanT, target: str) -> None:
        """Copy the agent's named skills from the host into the
        running bottle. No-op when the agent has no skills."""

    @abstractmethod
    def provision_ssh(self, plan: PlanT, target: str) -> None:
        """Set up SSH in the running bottle (config, agent, keys)
        so the bottle can reach the manifest's declared SSH hosts.
        No-op when the bottle has no SSH entries."""

    @abstractmethod
    def provision_git(self, plan: PlanT, target: str) -> None:
        """Copy the host's cwd `.git` directory into the running
        bottle if the user requested --cwd. No-op otherwise."""

    @abstractmethod
    def prepare_cleanup(self) -> CleanupT:
        """Enumerate orphaned resources from previous bottles. No side
        effects; safe to call before the y/N."""

    @abstractmethod
    def cleanup(self, plan: CleanupT) -> None:
        """Remove everything described by the cleanup plan."""

    @abstractmethod
    def list_active(self) -> None:
        """Print every currently-running bottle on this backend to
        stderr (name + status)."""


# Import concrete backend classes AFTER the base types are defined, so
# each backend module can pull BottleSpec / BottlePlan / BottleBackend
# via `from . import ...` without hitting a partially-initialized module.
from .docker import DockerBottleBackend  # noqa: E402


# The dict is heterogeneous: each value is a BottleBackend specialized
# over its own plan type. Concrete plan types are erased here because
# the registry is selected at runtime and the CLI only needs the
# unparameterized methods (prepare → plan → launch(plan), cleanup, etc.).
_BACKENDS: dict[str, BottleBackend[Any, Any]] = {
    "docker": DockerBottleBackend(),
}


def get_bottle_backend() -> BottleBackend[Any, Any]:
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
    "BottleSpec",
    "get_bottle_backend",
]
