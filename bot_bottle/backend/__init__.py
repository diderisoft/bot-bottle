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

Selection is driven by `--backend` on `start` or BOT_BOTTLE_BACKEND
(env var). When neither is set, compatible macOS hosts default to
`macos-container`; Linux hosts with KVM default to `firecracker`;
otherwise `docker`. Per PRD 0003 the manifest does not carry a
backend field; the host picks.
"""

from __future__ import annotations

import os
import shlex
import sys
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generator, Generic, Sequence, TypeVar

from ..agent_provider import AgentProvisionPlan, get_provider, build_agent_provision_plan
from ..egress import EgressPlan
from ..git_gate import GitGatePlan
from ..log import die, info, warn
from ..util import read_tty_line
from ..manifest import Manifest, ManifestIndex
from ..supervise import SupervisePlan
from ..util import expand_tilde
from ..env import resolve_env, ResolvedEnv
from ..workspace import WorkspacePlan, workspace_plan
from .print_util import print_multi, visible_agent_env_names
from .util import host_skill_dir

if TYPE_CHECKING:
    from .freeze import CommitCancelled, Freezer, get_freezer


@dataclass(frozen=True)
class BottleSpec:
    """CLI-supplied intent. Backend-agnostic — each backend's prepare
    step consumes it and produces its own backend-specific plan.
    Resolved values (image names, container name, scratch paths, runsc
    availability) live on the plan, not the spec."""

    manifest: ManifestIndex
    agent_name: str
    copy_cwd: bool
    user_cwd: str
    # PRD 0016 follow-up: when set, the backend's prepare step uses
    # this identity instead of minting a fresh one — the resume path
    # (`cli.py resume <identity>`) sets this to continue an existing
    # bottle's state. Empty string for a fresh `start`.
    identity: str = ""
    label: str = ""
    color: str = ""
    # Ordered bottle names selected at launch (issue #269). When non-empty
    # they are merged in order and replace the agent's `bottle:` field.
    bottle_names: tuple[str, ...] = ()
    # True when launched via --headless (no TTY, no interactive prompts).
    # The git-gate host-key preflight uses this to error rather than prompt.
    headless: bool = False
    # Image startup policy. "fresh" preserves the normal build path;
    # "cached" reuses the current local image/artifact without rebuilding.
    image_policy: str = "fresh"


@dataclass(frozen=True)
class BottlePlan(ABC):
    """Base output of a backend's prepare step. Concrete subclasses
    (e.g. DockerBottlePlan) add backend-specific resolved fields."""

    spec: BottleSpec
    manifest: Manifest
    stage_dir: Path
    git_gate_plan: GitGatePlan

    @property
    def guest_home(self) -> str:
        return self.agent_provision.guest_home

    @property
    def git_gate_insteadof_host(self) -> str:
        """Host (and optional port) used in git-gate insteadOf URLs.
        Docker uses the compose-network DNS alias; VM backends may
        override with an IP:port when the guest has no DNS."""
        return "git-gate"

    @property
    def git_gate_insteadof_scheme(self) -> str:
        """URL scheme for git-gate insteadOf rewrites. 'git' for
        Docker (git daemon); VM backends may override (e.g. 'http'
        over a published host port)."""
        return "git"
    egress_plan: EgressPlan
    supervise_plan: SupervisePlan | None
    agent_provision: AgentProvisionPlan

    @property
    def workspace_plan(self) -> WorkspacePlan:
        return workspace_plan(self.spec, guest_home=self.guest_home)

    def print(self) -> None:
        """Render the y/N preflight summary to stderr."""
        spec = self.spec
        manifest = self.manifest
        agent = manifest.agent
        bottle = manifest.bottle

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
        effective_bottles = (
            list(spec.bottle_names) if spec.bottle_names
            else ([agent.bottle] if agent.bottle else [])
        )
        print_multi("bottle          ", effective_bottles)

        identity = manifest.git_identity_summary()
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
    VM backend could populate it from any source that produces a
    returncode + captured streams."""

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
    # reach for a host daemon socket (issue #392).
    supports_nested_containers: bool = False

    def prepare(self, spec: BottleSpec, stage_dir: Path) -> PlanT:
        """Template method: run cross-backend host-side validation, then
        delegate to the subclass's `_resolve_plan` for the
        backend-specific resolution (names, scratch files, etc.). The
        validation step is enforced here so a future backend cannot
        accidentally skip it. No remote/runtime resources are created."""
        from .resolve_common import (
            merge_provision_env_vars,
            mint_slug,
            prepare_agent_state_dir,
            prepare_egress,
            prepare_git_gate,
            prepare_supervise,
            reject_nested_containers,
            resolve_manifest_dockerfile,
            write_launch_metadata,
        )

        manifest = self._validate(spec)

        if not self.supports_nested_containers:
            reject_nested_containers(self.name, manifest)

        self._preflight()

        from ..git_gate_host_key import preflight_host_keys
        manifest = preflight_host_keys(
            manifest,
            headless=spec.headless,
            home_md=spec.manifest.home_md,
        )

        manifest_bottle = manifest.bottle
        manifest_agent_provider = manifest_bottle.agent_provider
        agent_provider = get_provider(manifest_agent_provider.template)
        resolved_env = resolve_env(manifest)
        workspace = workspace_plan(spec, guest_home=agent_provider.guest_home)

        slug = mint_slug(spec)
        write_launch_metadata(slug, spec, compose_project="", backend=self.name)

        # Manifest may override the Dockerfile per-bottle; otherwise fall
        # back to the provider plugin's bundled Dockerfile (next to its
        # agent_provider.py module).
        if manifest_agent_provider.dockerfile:
            agent_dockerfile_path = resolve_manifest_dockerfile(
                manifest_agent_provider.dockerfile, spec,
            )
        else:
            agent_dockerfile_path = str(agent_provider.dockerfile)

        agent_dir, prompt_file = prepare_agent_state_dir(slug, manifest)

        agent_provision_plan = build_agent_provision_plan(
            template=manifest_agent_provider.template,
            dockerfile=agent_dockerfile_path,
            state_dir=agent_dir,
            instance_name=f"bot-bottle-{slug}",
            prompt_file=prompt_file,
            guest_env=self._build_guest_env(resolved_env),
            forward_host_credentials=manifest_agent_provider.forward_host_credentials,
            auth_token=manifest_agent_provider.auth_token,
            host_env=dict(os.environ),
            trusted_project_path=workspace.workdir,
            label=spec.label,
            color=spec.color,
            provider_settings=manifest_agent_provider.settings,
        )
        agent_provision_plan = merge_provision_env_vars(agent_provision_plan)
        egress_plan = prepare_egress(manifest_bottle, slug, agent_provision_plan)
        supervise_plan = prepare_supervise(manifest_bottle, slug)
        git_gate_plan = prepare_git_gate(manifest_bottle, slug)

        return self._resolve_plan(
            spec,
            manifest=manifest,
            slug=slug,
            resolved_env=resolved_env,
            agent_provision_plan=agent_provision_plan,
            egress_plan=egress_plan,
            supervise_plan=supervise_plan,
            git_gate_plan=git_gate_plan,
            stage_dir=stage_dir,
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
                      stage_dir: Path) -> PlanT:
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
        images = self._build_or_load_images(plan)
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

        Per PRD 0050 the per-provider steps (prompt, skills,
        declarative provision-plan apply, supervise MCP registration)
        live on the `AgentProvider` plugin. The backend only owns the
        steps that are about backend infrastructure (CA, workspace,
        git) and surfaces the supervise daemon URL its launch step
        knows about via `supervise_mcp_url`.

        PRD 0017: cred-proxy's agent-side dotfile rewrites (~/.npmrc,
        ~/.gitconfig insteadOf, tea config) are gone. Egress-proxy is
        on the agent's HTTP_PROXY path so every tool that respects
        HTTPS_PROXY (claude-code, git over HTTPS, npm, curl) is
        intercepted without per-tool reconfiguration."""
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

    def ensure_orchestrator(self) -> str:
        """Bring up this backend's per-host orchestrator + shared gateway
        (idempotent) and return the host-reachable control-plane URL.

        This is the backend-agnostic bring-up entry point: `launch` calls
        it as part of starting a bottle, and operator tools (`supervise`)
        call it to start the control plane on demand when none is running
        yet. Docker starts the orchestrator + gateway containers;
        firecracker boots the infra VM. Backends with no orchestrator
        (macos-container) die with a pointer — the default here."""
        die(f"backend {self.name!r} has no orchestrator control plane")

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
        doesn't fail when `cli.py list active` walks past
        firecracker."""

    @classmethod
    @abstractmethod
    def setup(cls) -> int:
        """Emit this backend's one-time host setup — privileged network
        pool, daemon bring-up, install pointers, etc. — as
        host-appropriate config or commands. Prints to stdout/stderr and
        returns a shell exit code (0 = nothing to report / success). A
        backend that needs no host setup prints a short note and returns
        0. Invoked generically by `./cli.py backend setup [--backend=…]`
        so operators can provision any backend without a
        backend-specific command. Classmethod (like `is_available`) —
        it's a host query, not per-bottle state."""

    @classmethod
    @abstractmethod
    def status(cls) -> int:
        """Report whether this backend's prerequisites are satisfied on
        the host — binaries, daemon reachability, network pool, range
        conflicts, etc. Prints a human-readable summary; returns 0 when
        the backend is ready to launch and non-zero when something is
        missing. Invoked by `./cli.py backend status [--backend=…]`."""

    @classmethod
    @abstractmethod
    def teardown(cls) -> int:
        """Undo `setup()` — the inverse operation, surfaced as
        `./cli.py backend teardown [--backend=…]` (uninstall). Symmetric
        with setup: where setup is advisory (prints the privileged
        commands / declarative config to apply), teardown prints the
        commands / config change to remove the host prerequisites. A
        backend with no host setup prints a short note and returns 0.
        Not called by the launch path or the test suite."""


# _backends is None until the first call to _get_backends(), at which
# point all three concrete backend classes are imported and instantiated.
# Keeping the imports out of module scope means that importing any
# backend sub-module (e.g. `backend.docker.util`) no longer drags the
# firecracker and macos-container implementations into memory.
#
# Tests may replace _backends with a {name: fake} dict via patch.object;
# _get_backends() returns the current module-level value as-is when it
# is not None, so test fakes take effect without triggering real imports.
_backends: dict[str, BottleBackend[Any, Any]] | None = None


def _get_backends() -> dict[str, BottleBackend[Any, Any]]:
    """Return the registry of all backend instances, loading lazily on first call."""
    global _backends  # pylint: disable=global-statement
    if _backends is None:
        from .docker import DockerBottleBackend
        from .firecracker import FirecrackerBottleBackend
        from .macos_container import MacosContainerBottleBackend
        _backends = {
            "docker": DockerBottleBackend(),
            "firecracker": FirecrackerBottleBackend(),
            "macos-container": MacosContainerBottleBackend(),
        }
    return _backends


def __getattr__(name: str) -> Any:
    """Lazily surface concrete backend classes and freeze symbols at the
    package level so existing `from bot_bottle.backend import X` and
    `patch.object(backend_mod, X, ...)` call-sites keep working without
    forcing an import of every backend at module-init time."""
    if name == "DockerBottleBackend":
        from .docker import DockerBottleBackend
        globals()[name] = DockerBottleBackend
        return DockerBottleBackend
    if name == "FirecrackerBottleBackend":
        from .firecracker import FirecrackerBottleBackend
        globals()[name] = FirecrackerBottleBackend
        return FirecrackerBottleBackend
    if name == "MacosContainerBottleBackend":
        from .macos_container import MacosContainerBottleBackend
        globals()[name] = MacosContainerBottleBackend
        return MacosContainerBottleBackend
    if name == "CommitCancelled":
        from .freeze import CommitCancelled
        globals()[name] = CommitCancelled
        return CommitCancelled
    if name == "Freezer":
        from .freeze import Freezer
        globals()[name] = Freezer
        return Freezer
    if name == "get_freezer":
        from .freeze import get_freezer
        globals()[name] = get_freezer
        return get_freezer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_bottle_backend(
    name: str | None = None,
    *,
    prompt: bool = True,
) -> BottleBackend[Any, Any]:
    """Resolve the bottle backend.

    `name` precedence:
      1. explicit arg (e.g. resume passes the recorded backend name)
      2. BOT_BOTTLE_BACKEND env var
      3. auto-selection: VM backend first, docker fallback with prompt

    `prompt` controls whether auto-selection may block on an interactive
    [i/d/q] prompt when falling back to docker. Pass `prompt=False` in
    non-interactive contexts (headless launches, CI) so the call dies
    with an actionable message instead of hanging.

    Dies with a pointer at the known backends if the chosen name
    isn't implemented."""
    resolved = name or os.environ.get("BOT_BOTTLE_BACKEND")
    if resolved is None:
        resolved = _auto_select_backend(prompt=prompt)
    backends = _get_backends()
    if resolved not in backends:
        known = ", ".join(sorted(backends))
        die(f"unknown backend {resolved!r}; known backends: {known}")
    return backends[resolved]


def _platform_vm_suggestion() -> str:
    """Platform-appropriate VM backend name for install suggestions."""
    return "macos-container" if sys.platform == "darwin" else "firecracker"


def _print_vm_install_instructions() -> None:
    """Print platform-appropriate VM backend install instructions to stderr."""
    vm = _platform_vm_suggestion()
    if vm == "macos-container":
        info("Install Apple Container: https://github.com/apple/container/releases")
        info("Then start the service:  container system start")
    else:
        info("Install Firecracker: https://github.com/firecracker-microvm/firecracker/releases")
        info("Configure the host:  ./cli.py backend setup")


def _auto_select_backend(prompt: bool = True) -> str:
    """Tier-1 / tier-2 backend auto-selection.

    Tier 1: VM backend — macos-container on macOS when Apple Container is
    installed; firecracker on KVM-capable Linux even before the binary is
    present (its preflight prints an install pointer).

    Tier 2: docker, with a security warning and an interactive prompt.
    When `prompt=False` (headless / CI), dies with an actionable message
    instead of blocking on a TTY read. When docker is also absent, prints
    VM install instructions and exits.
    """
    # --- Tier 1: VM backend -----------------------------------------
    if has_backend("macos-container"):
        return "macos-container"
    # A KVM-capable Linux host defaults to firecracker even when the
    # `firecracker` binary isn't installed yet: selecting it here routes
    # start through firecracker's preflight, which prints an install
    # pointer, instead of silently falling back to docker.
    from .firecracker import FirecrackerBottleBackend
    if FirecrackerBottleBackend.is_host_capable():
        return "firecracker"

    # --- Tier 2: docker fallback ------------------------------------
    if not has_backend("docker"):
        info("No backend available on this host.")
        _print_vm_install_instructions()
        die("no backend available; install a VM backend and re-run")

    vm = _platform_vm_suggestion()
    warn(
        "docker is less secure than VM backends — "
        "containers share the host kernel."
    )
    if not prompt:
        die(
            f"no VM backend available; set BOT_BOTTLE_BACKEND=docker to proceed "
            f"with docker, or install the {vm!r} backend."
        )
    sys.stderr.write(
        f"bot-bottle: For better isolation, install the {vm!r} backend.\n"
        f"  [i] show {vm} install instructions and exit\n"
        "  [d] use docker anyway\n"
        "  [q] quit\n"
        "bot-bottle: choice [i/d/q]: "
    )
    sys.stderr.flush()
    reply = read_tty_line().strip().lower()
    if reply == "d":
        return "docker"
    if reply == "i":
        _print_vm_install_instructions()
    die("not proceeding with docker; install a VM backend or set BOT_BOTTLE_BACKEND=docker")


def known_backend_names() -> tuple[str, ...]:
    """Sorted tuple of all backend keys in `_get_backends()`. Used by
    argparse (`--backend` choices) and the dashboard's backend
    picker."""
    return tuple(sorted(_get_backends()))


def has_backend(name: str) -> bool:
    """Whether the named backend's runtime prerequisites are
    available on the current host. Cross-backend callers (list,
    cleanup) skip unavailable backends so a docker-only host
    doesn't fail when the firecracker backend isn't usable,
    and vice versa.

    Returns False for unknown names so callers can pass
    arbitrary input without separate validation."""
    backends = _get_backends()
    if name not in backends:
        return False
    return backends[name].is_available()


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
    backends = _get_backends()
    for name in sorted(backends):
        if not backends[name].is_available():
            continue
        out.extend(backends[name].enumerate_active())
    out.sort(key=lambda a: (a.started_at, a.slug))
    return out


__all__ = [
    "ActiveAgent",
    "Bottle",
    "BottleBackend",
    "BottleCleanupPlan",
    "BottlePlan",
    "BottleSpec",
    "CommitCancelled",
    "ExecResult",
    "Freezer",
    "enumerate_active_agents",
    "get_bottle_backend",
    "get_freezer",
    "has_backend",
    "known_backend_names",
]
