"""The abstract backend contract.

The backend-neutral types every bottle backend implements: the launch
`BottleSpec` and `BottlePlan` (defined in `plan`, re-exported here), the
`BottleCleanupPlan` ABC, the running-`Bottle`
+ `ExecResult` shapes, `BottleImages`, and the `BottleBackend` ABC itself.

This carries the framework imports (manifest, egress, git-gate, env, workspace,
agent-provider) the contract's signatures and helpers need — which is why it
lives here rather than in `backend/__init__.py`: touching an unrelated
`backend.*` module then doesn't drag the whole framework into memory. The
thin package `__init__` re-exports these names lazily.
"""

from __future__ import annotations

import enum
import os
import shlex
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Generator, Generic, Sequence, TypeVar

from ..agent_provider import AgentProvisionPlan, get_provider
from ..contract.hostctl import Settings
from ..egress import EgressPlan
from ..git_gate import GitGatePlan
from ..lib import log
from ..lib.log import die, info
from ..util import expand_tilde
from ..manifest import Manifest
from ..secret_source import SecretSource
from ..supervisor.plan import SupervisePlan
from ..env import ResolvedEnv
from .plan import BottlePlan, BottleSpec
from .util import host_skill_dir

if TYPE_CHECKING:
    from .infra_service import InfraService


class BackendStatus(enum.IntEnum):
    """Return codes for BottleBackend.status(). READY == 0 so callsites
    can compare against 0 or the named constant interchangeably."""
    READY = 0


class EnumerationError(RuntimeError):
    """A backend could not produce an authoritative live-resource snapshot."""


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

    @abstractmethod
    def intersect(self, current: "BottleCleanupPlan") -> "BottleCleanupPlan":
        """Resources both displayed to the operator and currently removable."""


@dataclass(frozen=True)
class ExecResult:
    """Captured result of `Bottle.exec`. Backend-neutral: the Docker
    impl populates it from a `subprocess.CompletedProcess`, but a
    VM backend could populate it from any source that produces a
    returncode + captured streams."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ActiveAgent:
    """One currently-running agent, as the CLI `active` and
    dashboard agents pane render it. ("Agent" is the project's
    consistent name for the thing running inside a bottle — the
    bottle is the container, the agent is what runs in it.)

    Fields are deliberately backend-neutral. `services` is the set
    of gateway daemons currently up for this bottle (`egress`,
    `git-gate`, `supervise`); the dashboard uses it to
    gate edit verbs. `backend_name` is the matching key in
    `_BACKENDS` (`docker` / `firecracker` / `macos-container`) — used by the active-
    list rendering to disambiguate and by the dashboard's
    re-attach path."""

    backend_name: str
    slug: str
    agent_name: str       # from metadata.json; "?" if missing
    started_at: str       # ISO 8601 from metadata.json; "" if missing
    services: tuple[str, ...]  # alphabetical
    label: str = ""
    color: str = ""


class Bottle(ABC):
    """Handle to a running bottle. Yielded by a backend's launch step.

    `exec_agent` runs the selected agent CLI inside the bottle and
    blocks until the session ends. `exec` runs a POSIX shell script inside the bottle
    and returns the captured result. `cp_in` copies a host path into
    the bottle. `close` is an idempotent alias for context-manager
    teardown.
    """

    name: str
    # This run's id in the orchestrator registry, minted when it registered:
    # the audit journal's `activation`. Empty until the launch has registered.
    registry_id: str = ""

    @abstractmethod
    def agent_argv(
        self, argv: list[str], *, tty: bool = True,
    ) -> list[str]:
        """Return the host-side argv that runs the selected agent
        inside the bottle. Implementations transparently inject
        `--append-system-prompt-file` when the bottle was launched
        with a provisioned prompt path."""
        ...

    @abstractmethod
    def agent_command(self, argv: list[str]) -> tuple[list[str], dict[str, str]]:
        """The host command `exec_agent(argv, tty=True)` runs, terminal
        title/color wrapper included, and the env it adds to this
        process's. The host controller runs it in a tmux session."""

    @abstractmethod
    def exec_agent(self, argv: list[str], *, tty: bool = True) -> int: ...

    @abstractmethod
    def exec(self, script: str, *, user: str = "node") -> ExecResult:
        """Run `script` as a POSIX shell script inside the bottle as
        `user` (default `node`, matching the agent image's USER
        directive) and return the captured stdout/stderr/returncode.
        The bottle's environment (including HTTPS_PROXY pointing at
        the egress daemon) is inherited by the child. Non-zero
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


@dataclass(frozen=True)
class BottleImages:
    """Resolved image references (or artifact paths) for a bottle launch.

    For Docker/macOS-container backends, `agent` and `sidecar` are string
    image refs. For the smolmachines backend they are Path objects pointing
    to pre-built `.smolmachine` artifacts."""

    agent: str | Path
    sidecar: str | Path = ""


class BottleBackend(ABC, Generic[PlanT, CleanupT]):
    """Abstract base for selectable bottle backends. Concrete subclasses
    (e.g. DockerBottleBackend) own their own prepare/launch impls.
    Parameterized over the backend's concrete plan + cleanup-plan types
    so subclass methods get the narrow type without isinstance
    boilerplate."""

    name: str

    # Whether this backend can run a container engine *inside* the bottle.
    # Backends that cannot must reject `nested_containers: true` rather than
    # reach for a host daemon socket.
    supports_nested_containers: bool = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def prepare(self, spec: BottleSpec, stage_dir: Path, secrets: SecretSource) -> PlanT:
        """Template method: run cross-backend host-side validation, then
        delegate to the subclass's `_resolve_plan` for the
        backend-specific resolution (names, scratch files, etc.). The
        validation step is enforced here so a future backend cannot
        accidentally skip it. No remote/runtime resources are created.
        `secrets` supplies the bottle's secret values, now and at launch."""
        from .preparation import BottlePreparationPlanner
        prepared = BottlePreparationPlanner(self).prepare(spec, secrets)

        return self._resolve_plan(
            spec,
            manifest=prepared.manifest,
            slug=prepared.slug,
            resolved_env=prepared.resolved_env,
            agent_provision_plan=prepared.agent_provision_plan,
            egress_plan=prepared.egress_plan,
            supervise_plan=prepared.supervise_plan,
            git_gate_plan=prepared.git_gate_plan,
            stage_dir=stage_dir,
            secrets=secrets,
        )

    def _build_guest_env(self, resolved_env: ResolvedEnv) -> dict[str, str]:
        return {}

    def _preflight(self) -> None:
        """
        tasks to do before resolving a plan
        """
        pass

    def _validate(self, spec: BottleSpec) -> Manifest:
        """Cross-backend pre-launch checks. Parses the selected agent and
        its bottle (raising ManifestError on invalid content), confirms
        skills are present on the host, and every git IdentityFile resolves.

        Returns the loaded Manifest for the selected agent. Subclasses with
        additional preconditions should override and call
        `super()._validate(spec)` first."""
        manifest = spec.manifest.load_for_agent(spec.agent_name, spec.bottle_names)
        self._validate_skills(manifest.agent.skills)
        self._validate_agent_provider_dockerfile(spec, manifest)
        return manifest

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

    def _validate_agent_provider_dockerfile(self, spec: BottleSpec, manifest: Manifest) -> None:
        bottle = manifest.bottle
        dockerfile = bottle.agent_provider.dockerfile
        if not dockerfile:
            return
        path = Path(expand_tilde(dockerfile))
        if not path.is_absolute():
            path = Path(spec.user_cwd) / path
        if not path.is_file():
            effective = (
                ", ".join(spec.bottle_names) if spec.bottle_names else manifest.agent.bottle
            )
            die(
                f"agent_provider.dockerfile for bottle "
                f"'{effective}' not found: {path}"
            )

    @abstractmethod
    def _resolve_plan(self,
                      spec: BottleSpec,
                      *,
                      manifest: Manifest,
                      slug: str,
                      resolved_env: ResolvedEnv,
                      agent_provision_plan: AgentProvisionPlan,
                      egress_plan: EgressPlan,
                      git_gate_plan: GitGatePlan,
                      supervise_plan: SupervisePlan | None,
                      stage_dir: Path,
                      secrets: SecretSource) -> PlanT:
        """Backend-specific plan resolution: image/container names,
        env-file, prompt-file, proxy plan, runtime detection. Called by
        `prepare` after `_validate` succeeds. Instance name, image,
        prompt file, Dockerfile path, and guest home all live on
        `agent_provision_plan` — the source of truth."""

    def prelaunch_checks(self, plan: PlanT) -> None:
        """Raise StaleImageError if any cached image used by this plan is stale.
        No-op default; backends override to call the shared check_stale*
        helpers on their image/artifact timestamps. Called by the CLI before
        launch so the operator can be prompted outside the launch context."""

    @contextmanager
    def launch(self, plan: PlanT) -> Generator[Bottle, None, None]:
        """Template: build or load images, then delegate to _launch_impl."""
        log.stage("image", "Getting images")
        images = self._build_or_load_images(plan)
        # The backend's bring-up marks "Starting bot-bottle services" and then
        # "Starting the bottle" (InfraService.ensure_running).
        with self._launch_impl(plan, images) as bottle:
            yield bottle

    @abstractmethod
    def _build_or_load_images(self, plan: PlanT) -> BottleImages:
        """Return the agent and sidecar image references (or artifact paths)
        for this plan, building fresh images when the policy requires it."""

    @abstractmethod
    def _launch_impl(self, plan: PlanT, images: BottleImages) -> AbstractContextManager[Bottle]:
        """Bring up the bottle using pre-resolved images; yield a handle; tear down on exit."""

    def provision(self, plan: PlanT, bottle: "Bottle") -> str | None:
        """Copy host-side files (CA cert, prompt, skills, .git) into
        the running bottle. Called from `launch` after the container
        / machine is up. Returns the in-container prompt path if a
        prompt was provisioned, else None — the Bottle handle uses it
        to decide whether to add provider-specific prompt args to the
        agent's argv.

        Default orchestration: ca → prompt → provider apply → skills
        → workspace → git → supervise-mcp. CA install runs first so
        the agent's trust store is rebuilt before anything inside the
        agent makes a TLS call.

        The per-provider steps (prompt, skills,
        declarative provision-plan apply, supervise MCP registration)
        live on the `AgentProvider` plugin. The backend only owns the
        steps that are about backend infrastructure (CA, workspace,
        git) and surfaces the supervise daemon URL its launch step
        knows about via `supervise_mcp_url`.

        The egress proxy is on the agent's HTTP_PROXY path, so every tool
        that respects HTTPS_PROXY (claude-code, git over HTTPS, npm, curl)
        is intercepted without per-tool reconfiguration."""
        log.stage("provision", "Setting up the agent")
        provider = get_provider(plan.agent_provision.template)
        provider.provision_ca(bottle, plan)
        prompt_path = provider.provision_prompt(plan, bottle)
        provider.provision(plan, bottle)
        provider.provision_skills(plan, bottle)
        self.provision_workspace(plan, bottle)
        provider.provision_git(bottle, plan)
        provider.provision_supervise_mcp(
            plan, bottle, self.supervise_mcp_url(plan),
        )
        return prompt_path

    def provision_workspace(self, plan: PlanT, bottle: "Bottle") -> None:
        """Copy the operator workspace into the running bottle.

        This is the only supported workspace-provisioning path: Docker
        does not build a derived image containing the current
        workspace."""
        workspace = plan.workspace_plan
        if not (workspace.enabled and workspace.copy_contents):
            return

        guest_parent = workspace.guest_path.rsplit("/", 1)[0] or "/"
        guest_path = shlex.quote(workspace.guest_path)
        guest_parent = shlex.quote(guest_parent)
        owner = shlex.quote(workspace.owner)
        mode = shlex.quote(workspace.mode)
        info(f"copying {workspace.host_path} -> {bottle.name}:{workspace.guest_path}")
        bottle.exec(
            f"rm -rf {guest_path} && mkdir -p {guest_parent}",
            user="root",
        )
        bottle.cp_in(str(workspace.host_path), workspace.guest_path)
        bottle.exec(
            f"chown -R {owner} {guest_path} && chmod {mode} {guest_path}",
            user="root",
        )

    def supervise_mcp_url(self, plan: PlanT) -> str:
        """Return the agent-side URL of the per-bottle supervise
        gateway, or "" when this bottle has no gateway. The provider
        plugin's `provision_supervise_mcp` uses it to register the
        MCP entry inside the guest.

        Default returns "" so backends without supervise support
        don't have to implement it. Docker and firecracker override."""
        del plan
        return ""

    @abstractmethod
    def infra_service(self) -> "InfraService":
        """This backend's per-host orchestrator + gateway pair. The
        one hook a backend implements to get bring-up, reattaching running
        bottles after a restart, and CA rotation."""

    def ensure_orchestrator(self) -> str:
        """Bring up this backend's per-host orchestrator + shared gateway
        (idempotent) and return the host-reachable control-plane URL. `launch`
        and operator tools (`supervise`) both start the control plane here."""
        return self.infra_service().ensure_running()

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
        compose ls`; firecracker cross-references its running gateway
        containers against per-bottle metadata."""

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        """Whether this backend's runtime prerequisites are satisfied
        on the current host. Docker → `docker` on PATH; firecracker →
        Linux + KVM. Used by the cross-backend
        `enumerate_active_agents` / `cmd_cleanup` to skip backends
        the operator hasn't installed, so a docker-only host
        doesn't fail when `cli.py active` walks past
        firecracker."""

    @abstractmethod
    def setup(self) -> int:
        """Emit this backend's one-time host setup — privileged network
        pool, daemon bring-up, install pointers, etc. — as
        host-appropriate config or commands. Prints to stdout/stderr and
        returns a shell exit code (0 = nothing to report / success). A
        backend that needs no host setup prints a short note and returns
        0. Invoked generically by `bot-bottle backend setup [--backend=…]`
        so operators can provision any backend without a
        backend-specific command."""

    @abstractmethod
    def status(self, *, quiet: bool = False) -> int:
        """Report whether this backend's prerequisites are satisfied on
        the host — binaries, daemon reachability, network pool, range
        conflicts, etc. Returns BackendStatus.READY (0) when the backend
        is ready to launch and non-zero when something is missing.

        When quiet=False (default) prints a human-readable summary to
        stderr. When quiet=True returns the status code silently —
        useful for cheap programmatic checks.

        Invoked by `bot-bottle backend status [--backend=…]` (quiet=False)
        and by is_backend_ready() (caller-controlled)."""

    @abstractmethod
    def teardown(self) -> int:
        """Undo `setup()` — the inverse operation, surfaced as
        `bot-bottle backend teardown [--backend=…]` (uninstall). Symmetric
        with setup: where setup is advisory (prints the privileged
        commands / declarative config to apply), teardown prints the
        commands / config change to remove the host prerequisites. A
        backend with no host setup prints a short note and returns 0.
        Not called by the launch path or the test suite."""
