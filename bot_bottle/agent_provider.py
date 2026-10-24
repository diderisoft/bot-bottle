"""Agent provider plugins: the launch-time mapping from a provider harness to
its command, image, auth and guest setup.

Built-in providers live in `bot_bottle/contrib/<harness>/agent_provider.py`; a
user plugin in `~/.bot-bottle/contrib/<harness>/` shadows a built-in.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import shlex
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .egress import EgressRoute


if TYPE_CHECKING:
    from .backend import Bottle, BottlePlan


PROVIDER_CLAUDE = "claude"
PROVIDER_CODEX = "codex"
PROVIDER_PI = "pi"
BUILTIN_HARNESSES = frozenset({PROVIDER_CLAUDE, PROVIDER_CODEX, PROVIDER_PI})

# Hosts egress injects the host ChatGPT bearer on when Codex
# `forward_host_credentials` is enabled.
CODEX_HOST_CREDENTIAL_HOSTS = ("api.openai.com", "chatgpt.com")

PromptMode = Literal[
    "append_file",
    "read_prompt_file",
    "print_read_prompt_file",
    "append_system_prompt",
]


@dataclass(frozen=True)
class AgentProviderRuntime:
    harness: str
    command: str
    image: str
    prompt_mode: PromptMode
    bypass_args: tuple[str, ...]
    # argv run in a throwaway container of a freshly built image to catch a
    # build that exited 0 but produced a broken CLI. Empty skips the check.
    smoke_test: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentProvisionDir:
    guest_path: str
    mode: str = "700"
    owner: str = "node:node"


@dataclass(frozen=True)
class AgentProvisionFile:
    host_path: Path
    guest_path: str
    mode: str = "600"
    owner: str = "node:node"


@dataclass(frozen=True)
class AgentProvisionCommand:
    argv: tuple[str, ...]
    error: str = ""


@dataclass(frozen=True)
class AgentProvisionPlan:
    """Provider-owned guest setup, interpreted by each backend.

    `egress_routes` are merged into the egress plan. `hidden_env_names` are
    placeholder env names the preflight summary hides.
    """

    harness: str
    command: str
    prompt_mode: PromptMode
    image: str
    dockerfile: str
    guest_home: str
    instance_name: str
    prompt_file: Path
    guest_env: dict[str, str]
    has_prompt: bool = False
    startup_args: tuple[str, ...] = ()
    env_vars: dict[str, str] = field(default_factory=dict)
    dirs: tuple[AgentProvisionDir, ...] = ()
    files: tuple[AgentProvisionFile, ...] = ()
    pre_copy: tuple[AgentProvisionCommand, ...] = ()
    verify: tuple[AgentProvisionCommand, ...] = ()
    egress_routes: tuple[EgressRoute, ...] = ()
    hidden_env_names: frozenset[str] = field(default_factory=frozenset)
    provisioned_env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class HostDetection:
    """What a provider found on the host, for `setup` and `doctor`.

    `login_location` names where the login lives, never the secret.
    `starter_settings` is the `agent_provider.settings` a starter bottle
    carries."""

    installed: bool = False
    logged_in: bool = False
    login_location: str = ""
    login_command: str = ""
    starter_settings: Mapping[str, object] = field(default_factory=dict)


class AgentProvider(ABC):
    """Per-harness plugin: builds the provision plan and runs the provider's
    guest setup (skills, prompt, files, supervise MCP registration)."""

    @property
    @abstractmethod
    def runtime(self) -> AgentProviderRuntime:
        """Command, image and prompt-mode table for this harness."""

    @property
    def guest_home(self) -> str:
        """The agent user's home in the guest."""
        return "/home/node"

    @property
    def image_user(self) -> str:
        """The user the default image ends as. Overlays restore it after their
        RUN steps."""
        return "node"

    def harness_overlay_lines(self, version: str) -> tuple[str, ...] | None:
        """Dockerfile lines that install harness `version` in an overlay, or
        None if unsupported. They start as root and must fail the build unless
        the installed harness reports exactly `version`."""
        return None

    @property
    def dockerfile(self) -> Path:
        """Path to the provider's Dockerfile; defaults to the one next to its
        module."""
        return Path(inspect.getfile(type(self))).parent / "Dockerfile"

    def detect_host(self, host_env: Mapping[str, str]) -> HostDetection:
        """Whether this provider can run a starter on this host. Must not read
        secrets or prompt."""
        del host_env
        return HostDetection()

    def create_default_agent_manifest(self, detection: HostDetection) -> str:
        """Starter agent manifest (`agents/<harness>.md`)."""
        raise NotImplementedError(f"{type(self).__name__} has no starter agent")

    def create_default_bottle_manifest(self, detection: HostDetection) -> str:
        """Starter bottle manifest (`bottles/<harness>.md`) with the detected
        login."""
        raise NotImplementedError(f"{type(self).__name__} has no starter bottle")

    @abstractmethod
    def provision_plan(
        self,
        *,
        dockerfile: str,
        state_dir: Path,
        instance_name: str,
        prompt_file: Path,
        guest_env: dict[str, str] | None = None,
        auth_token: str = "",
        forward_host_credentials: bool = False,
        host_env: Mapping[str, str] | None = None,
        trusted_project_path: str = "",
        label: str = "",
        color: str = "",
        provider_settings: dict[str, object] | None = None,
    ) -> AgentProvisionPlan:
        """Build the AgentProvisionPlan for one launch.

        `auth_token` is a secret's name, never its value. `host_env` locates
        host credential files; None means this process's environment."""

    @abstractmethod
    def provision_skills(self, plan: "BottlePlan", bottle: "Bottle") -> None:
        """Copy the agent's skills from the host into the guest."""

    @abstractmethod
    def provision_prompt(self, plan: "BottlePlan", bottle: "Bottle") -> str | None:
        """Copy the prompt file into the guest and return its guest path iff
        the prompt is non-empty."""

    @abstractmethod
    def provision(self, plan: "BottlePlan", bottle: "Bottle") -> None:
        """Apply the plan's `dirs`/`pre_copy`/`files`/`verify` steps."""

    @abstractmethod
    def provision_supervise_mcp(
        self,
        plan: "BottlePlan",
        bottle: "Bottle",
        supervise_url: str,
    ) -> None:
        """Register the supervise server as an MCP server in the provider's
        config. No-op without a supervise plan."""

    @abstractmethod
    def headless_prompt(self, prompt: str) -> list[str]:
        """Agent CLI args that run `prompt` as the task of a headless session,
        appended after `bypass_args` and `startup_args`."""

    @staticmethod
    def positional_prompt(prompt: str) -> str:
        """`prompt` made safe as a positional argument: a leading `-` parses as
        an option and pi reads a leading `@` as a file. A leading space changes
        neither for the model."""
        return " " + prompt if prompt[:1] in ("-", "@") else prompt

    def interactive_prompt(self, prompt: str) -> list[str] | None:
        """Agent CLI args that start an interactive session with `prompt` as
        its first message, or None if unsupported."""
        del prompt

    def remote_view_args(self) -> tuple[str, ...]:
        """Extra agent CLI args for a session relayed to the console. Default:
        none."""
        return ()

    def provision_ca(self, bottle: "Bottle", plan: "BottlePlan") -> None:
        """Install the egress CA into the agent's trust store (Debian-style by
        default)."""
        from .backend.util import AGENT_CA_PATH, log_ca_fingerprint, select_ca_cert
        from .lib.log import die
        cert_host_path, label = select_ca_cert(plan.egress_plan)
        # Some rootfs builds drop the empty directory.
        bottle.exec("mkdir -p /usr/local/share/ca-certificates", user="root")
        bottle.cp_in(str(cert_host_path), AGENT_CA_PATH)
        r = bottle.exec(
            f"chmod 644 {AGENT_CA_PATH} && update-ca-certificates",
            user="root",
        )
        if r.returncode != 0:
            die(
                f"update-ca-certificates failed (exit {r.returncode}): "
                f"stdout={(r.stdout or '').strip()!r} "
                f"stderr={(r.stderr or '').strip()!r}"
            )
        log_ca_fingerprint(cert_host_path, label)

    def provision_git(self, bottle: "Bottle", plan: "BottlePlan") -> None:
        """Write the git-gate gitconfig and set the git identity in the guest."""
        from .lib.log import die, info

        # Firecracker's rootfs export doesn't preserve XDG directory ownership,
        # and an unreadable ~/.config/git can stop git from reading the
        # insteadOf rules.
        git_xdg_dir = f"{plan.guest_home}/.config/git"
        repair = bottle.exec(
            f"chown node:node {shlex.quote(plan.guest_home)} && "
            f"chmod 755 {shlex.quote(plan.guest_home)} && "
            f"mkdir -p {shlex.quote(git_xdg_dir)} && "
            f"chown -R node:node {shlex.quote(f'{plan.guest_home}/.config')} && "
            f"chmod -R u+rwX,go+rX {shlex.quote(f'{plan.guest_home}/.config')}",
            user="root",
        )
        if repair.returncode != 0:
            die(
                "git provisioning: could not make the runtime Git config "
                f"directory readable: {(repair.stderr or repair.stdout).strip()}"
            )

        manifest_bottle = plan.manifest.bottle
        if manifest_bottle.git:
            from .control.git_gate_render import git_gate_render_gitconfig
            content = git_gate_render_gitconfig(
                manifest_bottle.git, plan.git_gate_insteadof_host,
                identity_token=getattr(plan, "identity_token", ""),
            )
            guest_gitconfig = f"{plan.guest_home}/.gitconfig"
            with tempfile.NamedTemporaryFile(
                "w", dir=str(plan.stage_dir), prefix="gitconfig.", delete=False,
            ) as f:
                f.write(content)
                config_file = Path(f.name)
            os.chmod(config_file, 0o600)
            info(
                f"writing {guest_gitconfig} with "
                f"{len(manifest_bottle.git)} insteadOf rule(s)"
            )
            bottle.cp_in(str(config_file), guest_gitconfig)
            permissions = bottle.exec(
                f"chown node:node {shlex.quote(guest_gitconfig)} && "
                f"chmod 644 {shlex.quote(guest_gitconfig)}",
                user="root",
            )
            if permissions.returncode != 0:
                die(
                    "git provisioning: could not set ownership on "
                    f"{guest_gitconfig}: "
                    f"{(permissions.stderr or permissions.stdout).strip()}"
                )
            configured = bottle.exec(
                "git config --global --get-regexp '^url\\..*\\.insteadof$'",
                user="node",
            )
            if configured.returncode != 0:
                die(
                    "git provisioning: the runtime user cannot read the "
                    f"git-gate insteadOf rules from {guest_gitconfig}: "
                    f"{(configured.stderr or configured.stdout).strip()}"
                )

        gu = manifest_bottle.git_user
        if not gu.is_empty():
            if gu.name:
                info(f"git config --global user.name = {gu.name!r}")
                bottle.exec(
                    f"git config --global user.name {shlex.quote(gu.name)}",
                    user="node",
                )
            if gu.email:
                info(f"git config --global user.email = {gu.email!r}")
                bottle.exec(
                    f"git config --global user.email {shlex.quote(gu.email)}",
                    user="node",
                )


def _load_user_plugin(harness: str) -> AgentProvider | None:
    """A user-defined AgentProvider from ~/.bot-bottle/contrib/<harness>/, or
    None when there's no plugin directory. Raises ValueError if the file
    exports no subclass."""
    plugin_path = (
        Path.home() / ".bot-bottle" / "contrib" / harness / "agent_provider.py"
    )
    if not plugin_path.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        f"_user_contrib_{harness}.agent_provider", plugin_path
    )
    if spec is None or spec.loader is None:
        raise ValueError(f"user plugin at {plugin_path} could not be loaded")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    for obj in vars(mod).values():
        if (
            isinstance(obj, type)
            and issubclass(obj, AgentProvider)
            and obj is not AgentProvider
        ):
            return obj()
    raise ValueError(
        f"user plugin at {plugin_path} defines no AgentProvider subclass"
    )


def get_provider(harness: str) -> AgentProvider:
    """Resolve a harness name to its provider: a user plugin first, then the
    built-ins. Raises ValueError for an unknown name."""
    user_plugin = _load_user_plugin(harness)
    if user_plugin is not None:
        return user_plugin
    if harness == PROVIDER_CLAUDE:
        from .contrib.claude.agent_provider import ClaudeAgentProvider
        return ClaudeAgentProvider()
    if harness == PROVIDER_CODEX:
        from .contrib.codex.agent_provider import CodexAgentProvider
        return CodexAgentProvider()
    if harness == PROVIDER_PI:
        from .contrib.pi.agent_provider import PiAgentProvider
        return PiAgentProvider()
    raise ValueError(f"unknown agent provider harness: {harness!r}")


def runtime_for(harness: str) -> AgentProviderRuntime:
    return get_provider(harness).runtime


def build_agent_provision_plan(
    *,
    harness: str,
    dockerfile: str,
    state_dir: Path,
    instance_name: str,
    prompt_file: Path,
    guest_env: dict[str, str] | None = None,
    auth_token: str = "",
    forward_host_credentials: bool = False,
    host_env: Mapping[str, str] | None = None,
    trusted_project_path: str = "",
    label: str = "",
    color: str = "",
    provider_settings: dict[str, object] | None = None,
) -> AgentProvisionPlan:
    """Delegates to the provider plugin."""
    return get_provider(harness).provision_plan(
        dockerfile=dockerfile,
        state_dir=state_dir,
        instance_name=instance_name,
        prompt_file=prompt_file,
        guest_env=guest_env,
        auth_token=auth_token,
        forward_host_credentials=forward_host_credentials,
        host_env=host_env,
        trusted_project_path=trusted_project_path,
        label=label,
        color=color,
        provider_settings=provider_settings,
    )


def provider_startup_args(
    provider_settings: dict[str, object] | None,
) -> tuple[str, ...]:
    raw = (provider_settings or {}).get("startup_args", ())
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(arg for arg in raw if isinstance(arg, str))


def prompt_args(
    prompt_mode: PromptMode,
    prompt_path: str | None,
    *,
    argv: list[str] | None = None,
) -> list[str]:
    if not prompt_path:
        return []
    if prompt_mode == "append_file":
        return ["--append-system-prompt-file", prompt_path]
    if prompt_mode == "read_prompt_file":
        if argv and ("resume" in argv or "remote-control" in argv):
            return []
        return [f"Read and follow the instructions in {prompt_path}."]
    if prompt_mode == "print_read_prompt_file":
        return ["-p", f"Read and follow the instructions in {prompt_path}."]
    if prompt_mode == "append_system_prompt":
        return ["--append-system-prompt", prompt_path]
    raise ValueError(f"unknown provider prompt mode: {prompt_mode}")
