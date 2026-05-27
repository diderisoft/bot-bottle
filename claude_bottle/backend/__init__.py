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
from typing import Any, Generic, Sequence, TypeVar

from ..log import die
from ..manifest import GitEntry, Manifest
from ..util import expand_tilde
from .util import host_skill_dir


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
    # PRD 0016 follow-up: when set, the backend's prepare step uses
    # this identity instead of minting a fresh one — the resume path
    # (`cli.py resume <identity>`) sets this to continue an existing
    # bottle's state. Empty string for a fresh `start`.
    identity: str = ""


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


@dataclass(frozen=True)
class ExecResult:
    """Captured result of `Bottle.exec`. Backend-neutral: the Docker
    impl populates it from a `subprocess.CompletedProcess`, but a
    future fly/smolmachines backend could populate it from any source
    that produces a returncode + captured streams."""

    returncode: int
    stdout: str
    stderr: str


class Bottle(ABC):
    """Handle to a running bottle. Yielded by a backend's launch step.

    `exec_claude` runs `claude` inside the bottle and blocks until the
    session ends. `exec` runs a POSIX shell script inside the bottle
    and returns the captured result. `cp_in` copies a host path into
    the bottle. `close` is an idempotent alias for context-manager
    teardown.
    """

    name: str

    @abstractmethod
    def exec_claude(self, argv: list[str], *, tty: bool = True) -> int: ...

    @abstractmethod
    def exec(self, script: str, *, user: str = "node") -> ExecResult:
        """Run `script` as a POSIX shell script inside the bottle as
        `user` (default `node`, matching the agent image's USER
        directive) and return the captured stdout/stderr/returncode.
        The bottle's environment (including HTTPS_PROXY pointing at
        the pipelock sidecar) is inherited by the child. Non-zero
        exit does not raise — callers inspect `returncode`
        themselves.

        Pass `user="root"` for shell-outs that need privileged file
        writes / package install — provisioning calls that need root
        bypass `Bottle.exec` and use the backend-specific raw
        machine-exec helper, but the tests have a legitimate use
        case for arbitrary-user runs."""

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

    def prepare(self, spec: BottleSpec, *, stage_dir: Path) -> PlanT:
        """Template method: run cross-backend host-side validation, then
        delegate to the subclass's `_resolve_plan` for the
        backend-specific resolution (names, scratch files, etc.). The
        validation step is enforced here so a future backend cannot
        accidentally skip it. No remote/runtime resources are created."""
        self._validate(spec)
        return self._resolve_plan(spec, stage_dir=stage_dir)

    def _validate(self, spec: BottleSpec) -> None:
        """Cross-backend pre-launch checks. Confirms the agent exists,
        the named skills are present on the host, and every git
        IdentityFile resolves. Subclasses with additional preconditions
        should override and call `super()._validate(spec)` first."""
        manifest = spec.manifest
        manifest.require_agent(spec.agent_name)
        agent = manifest.agents[spec.agent_name]
        bottle = manifest.bottle_for(spec.agent_name)
        self._validate_skills(agent.skills)
        self._validate_git_entries(bottle.git)

    def _validate_skills(self, skills: Sequence[str]) -> None:
        """Each named skill must be a directory under the host's
        `~/.claude/skills/`. The check is purely host-side, so the
        default impl covers every backend."""
        for name in skills:
            path = host_skill_dir(name)
            if not os.path.isdir(path):
                die(
                    f"skill '{name}' not found on host at {path}. "
                    f"Create it under ~/.claude/skills/, then re-run."
                )

    def _validate_git_entries(self, entries: Sequence[GitEntry]) -> None:
        """Each entry's IdentityFile must exist on the host (after
        expanding leading ~) — the git-gate copies it in at start time
        to authenticate the upstream push (PRD 0008). Shape is already
        enforced by Manifest validation; this only checks presence."""
        for entry in entries:
            key = expand_tilde(entry.IdentityFile)
            if not os.path.isfile(key):
                die(f"git upstream key file not found for '{entry.Name}': {key}")

    @abstractmethod
    def _resolve_plan(self, spec: BottleSpec, *, stage_dir: Path) -> PlanT:
        """Backend-specific plan resolution: image/container names,
        env-file, prompt-file, proxy plan, runtime detection. Called by
        `prepare` after `_validate` succeeds."""

    @abstractmethod
    def launch(self, plan: PlanT) -> AbstractContextManager[Bottle]:
        """Build/run the bottle and yield a handle; tear down on exit."""

    def provision(self, plan: PlanT, target: str) -> str | None:
        """Copy host-side files (CA cert, prompt, skills, .git) into
        the running bottle. Called from `launch` after the container
        / machine is up. `target` identifies the running instance in
        backend-specific terms (Docker: resolved container name; fly:
        machine id). Returns the in-container prompt path if a prompt
        was provisioned, else None — the Bottle handle uses it to
        decide whether to add --append-system-prompt-file to claude's
        argv.

        Default orchestration: ca → prompt → skills → git →
        supervise. CA install runs first so the agent's trust store
        is rebuilt before anything inside the agent makes a TLS call.
        Subclasses typically don't override this; they implement the
        sub-methods below.

        PRD 0017: cred-proxy's agent-side dotfile rewrites (~/.npmrc,
        ~/.gitconfig insteadOf, tea config) are gone. Egress-proxy is
        on the agent's HTTP_PROXY path so every tool that respects
        HTTPS_PROXY (claude-code, git over HTTPS, npm, curl) is
        intercepted without per-tool reconfiguration."""
        self.provision_ca(plan, target)
        prompt_path = self.provision_prompt(plan, target)
        self.provision_skills(plan, target)
        self.provision_git(plan, target)
        self.provision_supervise(plan, target)
        return prompt_path

    def provision_ca(self, plan: PlanT, target: str) -> None:
        """Install the per-bottle CA into the agent's trust store so
        the agent trusts the bumped CONNECT cert egress (was
        pipelock, pre-PRD-0017) presents. Default impl is a no-op so
        backends that don't yet support TLS interception (every backend
        except Docker today) aren't forced to implement it. The Docker
        backend overrides to docker-cp the cert in and run
        `update-ca-certificates`."""

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
    def provision_git(self, plan: PlanT, target: str) -> None:
        """Copy the host's cwd `.git` directory into the running
        bottle if the user requested --cwd. No-op otherwise."""

    def provision_supervise(self, plan: PlanT, target: str) -> None:
        """Write the in-bottle Claude Code MCP config so the agent
        discovers the per-bottle supervise sidecar (PRD 0013).
        No-op when bottle.supervise is False or the backend doesn't
        support the supervise sidecar yet. The Docker backend
        overrides."""

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
from .smolmachines import SmolmachinesBottleBackend  # noqa: E402


# The dict is heterogeneous: each value is a BottleBackend specialized
# over its own plan type. Concrete plan types are erased here because
# the registry is selected at runtime and the CLI only needs the
# unparameterized methods (prepare → plan → launch(plan), cleanup, etc.).
_BACKENDS: dict[str, BottleBackend[Any, Any]] = {
    "docker": DockerBottleBackend(),
    "smolmachines": SmolmachinesBottleBackend(),
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
    "ExecResult",
    "get_bottle_backend",
]
