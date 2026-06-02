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

  enumerate_active() -> Sequence[ActiveAgent]
      Return every currently-running bottle on this backend, with
      enough metadata for callers (CLI `list active`, dashboard
      agents pane) to render a row.

Selection is driven by `--backend` on `start` or
BOT_BOTTLE_BACKEND (env var; default "docker"). Per PRD 0003 the
manifest does not carry a backend field; the host picks.
"""

from __future__ import annotations

import os
import sys
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Sequence, TypeVar

from ..agent_provider import AgentProvisionPlan
from ..egress import EgressPlan
from ..git_gate import GitGatePlan
from ..log import die, info
from ..manifest import GitEntry, Manifest
from ..supervise import SupervisePlan
from ..util import expand_tilde
from ..workspace import WorkspacePlan
from .print_util import print_multi, visible_agent_env_names
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
    (e.g. DockerBottlePlan) add backend-specific resolved fields."""

    spec: BottleSpec
    stage_dir: Path
    git_gate_plan: GitGatePlan
    egress_plan: EgressPlan
    supervise_plan: SupervisePlan | None
    agent_provision: AgentProvisionPlan
    workspace_plan: WorkspacePlan

    def print(self, *, remote_control: bool) -> None:
        """Render the y/N preflight summary to stderr."""
        del remote_control
        spec = self.spec
        manifest = spec.manifest
        agent = manifest.agents[spec.agent_name]
        bottle = manifest.bottle_for(spec.agent_name)

        env_names = visible_agent_env_names(
            sorted(
                set(bottle.env.keys())
                | set(self.agent_provision.guest_env.keys())
            ),
            hidden_env_names=self.agent_provision.hidden_env_names,
        )

        print(file=sys.stderr)
        info(f"agent           : {spec.agent_name}")
        info(f"provider        : {self.agent_provision.template}")
        print_multi("env             ", env_names)
        print_multi("skills          ", list(agent.skills))
        info(f"bottle          : {agent.bottle}")

        identity = manifest.git_identity_summary(spec.agent_name)
        if identity:
            info(f"  git identity  : {identity}")

        git_lines = [
            f"{u.name} → {u.upstream_host}:{u.upstream_port}"
            for u in self.git_gate_plan.upstreams
        ]
        if git_lines:
            print_multi("  git gate      ", git_lines)

        if self.egress_plan.routes:
            egress_lines = []
            for r in self.egress_plan.routes:
                auth = f" [auth:{r.auth_scheme}]" if r.auth_scheme else ""
                egress_lines.append(f"{r.host}{auth}")
            print_multi("  egress        ", egress_lines)
        print(file=sys.stderr)


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


@dataclass(frozen=True)
class ActiveAgent:
    """One currently-running agent, as the CLI `list active` and
    dashboard agents pane render it. ("Agent" is the project's
    consistent name for the thing running inside a bottle — the
    bottle is the container, the agent is what runs in it.)

    Fields are deliberately backend-neutral. `services` is the set
    of sidecar daemons currently up for this bottle (`pipelock`,
    `egress`, `git-gate`, `supervise`); the dashboard uses it to
    gate edit verbs. `backend_name` is the matching key in
    `_BACKENDS` (`docker` / `smolmachines`) — used by the active-
    list rendering to disambiguate and by the dashboard's
    re-attach path."""

    backend_name: str
    slug: str
    agent_name: str       # from metadata.json; "?" if missing
    started_at: str       # ISO 8601 from metadata.json; "" if missing
    services: tuple[str, ...]  # alphabetical


class Bottle(ABC):
    """Handle to a running bottle. Yielded by a backend's launch step.

    `exec_agent` runs the selected agent CLI inside the bottle and
    blocks until the session ends. `exec` runs a POSIX shell script inside the bottle
    and returns the captured result. `cp_in` copies a host path into
    the bottle. `close` is an idempotent alias for context-manager
    teardown.
    """

    name: str

    @abstractmethod
    def agent_argv(
        self, argv: list[str], *, tty: bool = True,
    ) -> list[str]:
        """Return the host-side argv that runs the selected agent
        inside the bottle. Used by `exec_agent` for foreground
        handoffs and by the dashboard's tmux `respawn-pane` flow,
        which needs the argv up front (it spawns claude in a tmux
        pane rather than as a child of the current process).

        Implementations transparently inject
        `--append-system-prompt-file` when the bottle was launched
        with a provisioned prompt path."""
        ...

    @abstractmethod
    def exec_agent(self, argv: list[str], *, tty: bool = True) -> int: ...

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
        self._validate_agent_provider_dockerfile(spec)

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

    def _validate_agent_provider_dockerfile(self, spec: BottleSpec) -> None:
        bottle = spec.manifest.bottle_for(spec.agent_name)
        dockerfile = bottle.agent_provider.dockerfile
        if not dockerfile:
            return
        path = Path(expand_tilde(dockerfile))
        if not path.is_absolute():
            path = Path(spec.user_cwd) / path
        if not path.is_file():
            die(
                f"agent_provider.dockerfile for bottle "
                f"'{spec.manifest.agents[spec.agent_name].bottle}' not found: {path}"
            )

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
        decide whether to add provider-specific prompt args to the agent's
        argv.

        Default orchestration: ca → prompt → skills → workspace → git →
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
        self.provision_provider_auth(plan, target)
        self.provision_skills(plan, target)
        self.provision_workspace(plan, target)
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

    def provision_provider_auth(self, plan: PlanT, target: str) -> None:
        """Install non-secret provider auth marker files into the agent
        home when a provider needs them to select the right auth mode.
        The default is no-op."""

    @abstractmethod
    def provision_prompt(self, plan: PlanT, target: str) -> str | None:
        """Copy the prompt file into the running bottle. Returns the
        in-container path iff the agent has a non-empty prompt;
        callers use the return value to decide whether to add
        provider-specific prompt args to the agent's argv."""

    @abstractmethod
    def provision_skills(self, plan: PlanT, target: str) -> None:
        """Copy the agent's named skills from the host into the
        running bottle. No-op when the agent has no skills."""

    def provision_workspace(self, plan: PlanT, target: str) -> None:
        """Copy the operator workspace into the running bottle when
        the backend cannot bake it into the agent image. Default is
        no-op for backends like Docker that handle this before launch."""

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
    def enumerate_active(self) -> Sequence[ActiveAgent]:
        """Return every currently-running agent on this backend.
        Empty when none. Backend-specific: docker queries `docker
        compose ls`; smolmachines queries `smolvm machine ls --json`
        + cross-references its bundle container."""

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        """Whether this backend's runtime prerequisites are satisfied
        on the current host. Docker → `docker` on PATH; smolmachines
        → `smolvm` on PATH. Used by the cross-backend
        `enumerate_active_agents` / `cmd_cleanup` to skip backends
        the operator hasn't installed, so a docker-only host
        doesn't fail when `cli.py list active` walks past
        smolmachines."""


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


def get_bottle_backend(
    name: str | None = None,
) -> BottleBackend[Any, Any]:
    """Resolve the bottle backend.

    `name` precedence:
      1. explicit arg (CLI `--backend=<name>` passes through here)
      2. BOT_BOTTLE_BACKEND env var
      3. default `docker`

    Dies with a pointer at the known backends if the chosen name
    isn't implemented."""
    resolved = name or os.environ.get("BOT_BOTTLE_BACKEND") or "docker"
    if resolved not in _BACKENDS:
        known = ", ".join(sorted(_BACKENDS))
        die(f"unknown backend {resolved!r}; known backends: {known}")
    return _BACKENDS[resolved]


def known_backend_names() -> tuple[str, ...]:
    """Sorted tuple of all backend keys in `_BACKENDS`. Used by
    argparse (`--backend` choices) and the dashboard's backend
    picker."""
    return tuple(sorted(_BACKENDS))


def has_backend(name: str) -> bool:
    """Whether the named backend's runtime prerequisites are
    available on the current host. Cross-backend callers (list,
    cleanup) skip unavailable backends so a docker-only host
    doesn't fail when the smolmachines backend isn't installed,
    and vice versa.

    Returns False for unknown names so callers can pass
    arbitrary input without separate validation."""
    if name not in _BACKENDS:
        return False
    return _BACKENDS[name].is_available()


def enumerate_active_agents() -> list[ActiveAgent]:
    """All currently-running agents, across every available
    backend. Used by CLI `list active` and the dashboard's agents
    pane so neither has to know which backends exist. Skips
    backends whose `is_available()` reports False.

    Sorted by `(started_at, slug)` so the list is stable across
    dashboard refresh ticks — agents don't shift position while
    the operator navigates with arrow keys. ISO 8601 timestamps
    sort lexicographically in chronological order; `slug` is the
    deterministic tiebreaker. Agents with missing metadata
    (`started_at == ""`) sort first."""
    out: list[ActiveAgent] = []
    for name in known_backend_names():
        if not has_backend(name):
            continue
        out.extend(_BACKENDS[name].enumerate_active())
    out.sort(key=lambda a: (a.started_at, a.slug))
    return out


__all__ = [
    "ActiveAgent",
    "Bottle",
    "BottleBackend",
    "BottleCleanupPlan",
    "BottlePlan",
    "BottleSpec",
    "ExecResult",
    "enumerate_active_agents",
    "get_bottle_backend",
    "has_backend",
    "known_backend_names",
]
