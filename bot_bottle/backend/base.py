"""The abstract backend contract: `BottleSpec`/`BottlePlan` (from `plan`),
`BottleCleanupPlan`, `Bottle`, `ExecResult`, `BottleImages` and
`BottleBackend`.

Kept out of `backend/__init__.py` because it imports the framework.
"""

from __future__ import annotations

import enum
import os
import shlex
import subprocess
import tempfile
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Generator, Generic, Sequence, TypeVar

from ..agent_provider import AgentProvisionPlan, get_provider
from ..contract.hostctl import Settings
from ..egress import EgressPlan
from ..git_gate import GitGatePlan
from ..lib import log
from ..lib.log import die, info, warn
from ..util import expand_tilde
from ..manifest import Manifest
from ..secret_source import SecretSource
from ..supervisor.plan import SupervisePlan
from ..workspace import LARGE_WORKSPACE_BYTES, pack_workspace, record_base_script
from ..env import ResolvedEnv
from .plan import BottlePlan, BottleSpec
from .util import host_skill_dir

if TYPE_CHECKING:
    from .cache_entries import CacheEntry
    from .host_resources import HostResource
    from .adoption import AdoptionRecord
    from .infra_service import InfraService


class BackendStatus(enum.IntEnum):
    """Return codes for `BottleBackend.status()`; READY is 0."""
    READY = 0


class EnumerationError(RuntimeError):
    """A backend couldn't produce an authoritative live-resource snapshot."""


@dataclass(frozen=True)
class BottleCleanupPlan(ABC):
    """A backend's `prepare_cleanup` result: the resources it would remove."""

    @abstractmethod
    def print(self) -> None:
        """Print the cleanup y/N summary to stderr."""

    @property
    @abstractmethod
    def empty(self) -> bool:
        """True iff there is nothing to clean up."""

    @abstractmethod
    def intersect(self, current: "BottleCleanupPlan") -> "BottleCleanupPlan":
        """Resources both displayed to the operator and currently removable."""


@dataclass(frozen=True)
class ExecResult:
    """Captured result of `Bottle.exec`."""

    returncode: int
    stdout: str
    stderr: str


# The exit status of a timed-out exec, as `timeout(1)` gives. The host
# controller bounds its background execs: one that never returned (a wedged
# `container exec`) held its caller, and whatever waited on it, for good.
TIMED_OUT = 124


def run_script(argv: list[str], script: str, *, timeout: float | None = None,
               env: dict[str, str] | None = None) -> ExecResult:
    """Run `argv` with `script` on its stdin, capturing its output. Past
    `timeout` seconds it's killed and the result is `TIMED_OUT`."""
    try:
        result = subprocess.run(argv, input=script, capture_output=True, text=True,
                                env=env, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ExecResult(returncode=TIMED_OUT, stdout="",
                          stderr=f"timed out after {timeout:g}s")
    return ExecResult(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)


def home_volume_name(slug: str) -> str:
    """The volume a bottled agent's home lives on, kept across stop and resume
    while its state is (PRD 0110)."""
    return f"bot-bottle-home-{slug}"


@dataclass(frozen=True)
class ActiveAgent:
    """One running agent, as `active` renders it. `services` is the gateway
    services up for it (`egress`, `git-gate`, `supervise`); `backend_name` is
    its backend's name."""

    backend_name: str
    slug: str
    agent_name: str  # from metadata.json; "?" if missing
    started_at: str  # ISO 8601 from metadata.json; "" if missing
    services: tuple[str, ...]  # alphabetical
    label: str = ""
    color: str = ""


class Bottle(ABC):
    """Handle to a running bottle, yielded by a backend's launch step. `close`
    is an idempotent alias for context-manager teardown.
    """

    name: str
    # This run's orchestrator registry id (the audit journal's `activation`).
    # Empty until registered.
    registry_id: str = ""
    # The key its egress tokens are stored under, for refreshing one; empty
    # until registered, and for a bottle a previous host controller launched.
    env_var_secret: str = ""
    # Whether its home (`home_volume_name`) held an earlier run of this bottled
    # agent: its conversation and workspace are there, so the workspace isn't
    # copied again and the agent continues its last conversation.
    home_kept: bool = False

    @abstractmethod
    def agent_argv(
        self, argv: list[str], *, tty: bool = True,
    ) -> list[str]:
        """Host argv that runs the agent in the bottle, with the provider's
        prompt args when a prompt was provisioned."""
        ...

    @abstractmethod
    def agent_command(self, argv: list[str]) -> tuple[list[str], dict[str, str]]:
        """The host command and added env that run the agent interactively;
        the host controller runs it in tmux."""

    @abstractmethod
    def exec(self, script: str, *, user: str = "node", timeout: float | None = None,
             private: bool = True) -> ExecResult:
        """Run `script` as a POSIX shell script in the bottle as `user` and
        return the captured result. Non-zero exit doesn't raise; past
        `timeout` seconds it's killed (`TIMED_OUT`). The script goes on
        stdin, off the command line any host process can read; a script with
        nothing secret in it (`private=False`) may go as an argument."""

    @abstractmethod
    def cp_in(self, host_path: str, container_path: str) -> None: ...

    @abstractmethod
    def close(self) -> None: ...


PlanT = TypeVar("PlanT", bound=BottlePlan)
CleanupT = TypeVar("CleanupT", bound=BottleCleanupPlan)


@dataclass(frozen=True)
class BottleImages:
    """Resolved image references or artifact paths for a launch."""

    agent: str | Path


# The interface every backend implements; each method is a hook the launcher,
# the host controller or the CLI calls.
class BottleBackend(ABC, Generic[PlanT, CleanupT]):  # pylint: disable=too-many-public-methods
    """Abstract base for bottle backends, parameterized over the concrete plan
    and cleanup-plan types."""

    name: str

    # Whether a container engine can run inside the bottle. Backends that can't
    # must reject `nested_containers: true`.
    supports_nested_containers: bool = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def prepare(self, spec: BottleSpec, stage_dir: Path, secrets: SecretSource) -> PlanT:
        """Run the shared host-side validation, then the subclass's
        `_resolve_plan`. Creates no runtime resources. `secrets` supplies
        secret values now and at launch."""
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
        """Host checks to run before resolving a plan."""
        pass

    def _validate(self, spec: BottleSpec) -> Manifest:
        """Shared pre-launch checks: parse the agent and its bottle, check
        skills exist and every git IdentityFile resolves. Returns the Manifest.
        Overrides call `super()._validate(spec)` first."""
        manifest = spec.manifest.load_for_agent(spec.agent_name, spec.bottle_names)
        self._validate_skills(manifest.agent.skills)
        self._validate_agent_provider_dockerfile(spec, manifest)
        return manifest

    def _validate_skills(self, skills: Sequence[str]) -> None:
        """Each named skill must be a directory under the host's
        `~/.claude/skills/`."""
        for name in skills:
            path = host_skill_dir(name)
            if not os.path.isdir(path):
                die(
                    f"skill '{name}' not found on host at {path}. "
                    f"Create it under ~/.claude/skills/, then re-run."
                )

    def _validate_agent_provider_dockerfile(self, spec: BottleSpec, manifest: Manifest) -> None:
        dockerfile = manifest.agent.agent_provider.dockerfile
        if not dockerfile:
            return
        path = Path(expand_tilde(dockerfile))
        if not path.is_absolute():
            path = Path(spec.user_cwd) / path
        if not path.is_file():
            die(f"agent '{spec.agent_name}' agent_provider.dockerfile not found: {path}")

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
        """Backend-specific plan resolution, after `_validate`. Instance name,
        image, prompt file and guest home live on `agent_provision_plan`."""

    def prelaunch_checks(self, plan: PlanT) -> None:
        """Raise StaleImageError if a cached image this plan uses is stale.
        Called by the CLI before launch, so it can prompt. No-op by default."""

    @contextmanager
    def launch(self, plan: PlanT) -> Generator[Bottle, None, None]:
        """Build or load images, then run `_launch_impl`."""
        log.stage("image", "Getting images")
        images = self._build_or_load_images(plan)
        with self._launch_impl(plan, images) as bottle:
            yield bottle

    @abstractmethod
    def _build_or_load_images(self, plan: PlanT) -> BottleImages:
        """Agent and gateway image references (or artifact paths), built when
        the policy requires."""

    @abstractmethod
    def _launch_impl(self, plan: PlanT, images: BottleImages) -> AbstractContextManager[Bottle]:
        """Bring up the bottle from resolved images; yield a handle; tear down
        on exit."""

    def provision(self, plan: PlanT, bottle: "Bottle") -> str | None:
        """Copy host files into the running bottle and return the guest prompt
        path, or None.

        Order: CA, prompt, provider steps, skills, workspace, git, supervise
        MCP. The CA goes first so the trust store is rebuilt before any TLS
        call. Provider-specific steps live on the `AgentProvider`."""
        log.stage("provision", "Setting up the agent")
        provider = get_provider(plan.agent_provision.harness)
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
        """Copy the operator workspace into the running bottle, unless its home
        was kept: the agent's copy, with its changes, is already there (and the
        copy starts with `rm -rf`)."""
        workspace = plan.workspace_plan
        if not (workspace.enabled and workspace.copy_contents):
            return
        if getattr(bottle, "home_kept", False) is True:
            info(f"keeping the workspace from {bottle.name}'s last run")
            return

        guest_path = shlex.quote(workspace.guest_path)
        owner = shlex.quote(workspace.owner)
        mode = shlex.quote(workspace.mode)
        is_git = workspace.has_host_git_dir
        # One filtered tarball, copied and unpacked in the guest.
        with tempfile.TemporaryDirectory(prefix="bot-bottle-ws.") as tmp:
            tarball = Path(tmp) / "workspace.tar"
            size = pack_workspace(workspace.host_path, tarball, is_git=is_git)
            info(f"copying {workspace.host_path} ({size >> 20} MB"
                 f"{', filtered by .gitignore' if is_git else ''}) -> "
                 f"{bottle.name}:{workspace.guest_path}")
            if not is_git and size > LARGE_WORKSPACE_BYTES:
                warn(f"{workspace.host_path} isn't a git repo and is {size >> 30} GB; "
                     "copying it all into the bottle")
            guest_tar = "/tmp/bot-bottle-workspace.tar"
            bottle.cp_in(str(tarball), guest_tar)
        unpacked = bottle.exec(
            f"rm -rf {guest_path} && mkdir -p {guest_path} && "
            f"tar -xf {guest_tar} -C {guest_path} && rm -f {guest_tar} && "
            f"chown -R {owner} {guest_path} && chmod {mode} {guest_path}",
            user="root",
        )
        if unpacked.returncode != 0:
            die(f"copying the workspace into {bottle.name} failed: "
                f"{unpacked.stderr.strip() or 'tar failed'}")
        if is_git:
            bottle.exec(record_base_script(guest_path))

    def supervise_mcp_url(self, plan: PlanT) -> str:
        """The agent-side URL of the supervise server, or "" when there is
        none."""
        return plan.agent_supervise_url

    def remove_home(self, slug: str) -> None:
        """Remove the bottled agent's home (`home_volume_name`), once its
        state isn't kept. Idempotent. Nothing by default."""
        del slug

    @abstractmethod
    def infra_service(self) -> "InfraService":
        """This backend's orchestrator + gateway pair: bring-up, reattach and
        CA rotation all go through it."""

    @abstractmethod
    def adopted_bottle(
        self, bottle_id: str, record: "AdoptionRecord",
    ) -> AbstractContextManager[Bottle]:
        """A running bottle a previous host controller launched: yields a
        `Bottle` to exec into and, on exit, tears it down as its holder would
        have, minus the deploy keys (the caller revokes those)."""

    def attach_adopted(self, bottle_id: str, record: "AdoptionRecord") -> Bottle | None:
        """A running bottle a previous host controller launched, to exec into
        without tearing it down; None when this backend can't."""
        del bottle_id, record
        return None

    def hold_adopted(self, bottle_id: str) -> "Callable[[], None] | None":
        """After a host controller restart, re-take what a running bottle needs
        held in the controller's process, before anything new launches.
        Returns how to let it go when the bottle ends, or None when there's
        nothing to hold."""
        del bottle_id
        return None

    @abstractmethod
    def prepare_cleanup(self) -> CleanupT:
        """Find orphaned resources from earlier bottles. No side effects."""

    @abstractmethod
    def cleanup(self, plan: CleanupT) -> None:
        """Remove everything in the cleanup plan."""

    @abstractmethod
    def enumerate_active(self) -> Sequence[ActiveAgent]:
        """Every running agent on this backend."""

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        """Whether this backend's runtime prerequisites are met on this host.
        Cross- backend listing and cleanup skip backends that aren't."""

    @abstractmethod
    def setup(self) -> int:
        """Print this backend's one-time host setup (network pool, daemons,
        install pointers) and return a shell exit code. Run by `bot-bottle
        install`."""

    @abstractmethod
    def status(self, *, quiet: bool = False) -> int:
        """Whether the backend's prerequisites are met: `BackendStatus.READY`
        (0) or non-zero. Prints a summary to stderr unless `quiet`."""

    @abstractmethod
    def teardown(self) -> int:
        """Print how to undo `setup()` and return a shell exit code. Run by
        `bot-bottle install --undo`."""

    def cache_entries(self) -> "list[CacheEntry]":
        """The cached images this backend keeps on the host (PRD 0088), for
        `bot-bottle cleanup images`. None by default."""
        return []

    def prune_build_store(self, *, dry_run: bool) -> tuple[int, str] | None:
        """Empty this backend's image build store, if it has one (`prune
        --build-store`): (bytes freed, a line to report). None when it has none."""
        del dry_run
        return None

    def host_resources(self, *, purge: bool) -> "list[HostResource]":
        """What this backend left on the host besides bottles (infra
        containers, networks, volumes, images), for `bot-bottle uninstall`.
        Frozen bottles' images only with `purge`. None by default."""
        del purge
        return []
