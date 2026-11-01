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
from collections.abc import Iterable, Mapping
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .egress import EgressRoute
from .paths import bot_bottle_root
from .transcript import TranscriptEntry


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
    `starter_settings` is the `agent_provider.settings` a starter agent
    carries."""

    installed: bool = False
    logged_in: bool = False
    login_location: str = ""
    login_command: str = ""
    starter_settings: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CredentialRefreshSpec:
    """What refreshing a bottle's forwarded host login takes later, none of it
    secret, so the adoption record can keep it for a restarted host
    controller: the `token_ref`s carrying the login, and a guest directory the
    provider keeps credential files in, if any."""

    harness: str
    token_refs: tuple[str, ...]
    guest_dir: str = ""


@dataclass(frozen=True)
class CredentialRefresh:
    """A forwarded host login as it is now: each `token_ref`'s value, and when
    the login expires (None if it doesn't say)."""

    values: Mapping[str, str]
    expires_at: datetime | None = None


class AgentProvider(ABC):  # pylint: disable=too-many-public-methods  # the plugin interface
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

    def continue_args(self) -> tuple[str, ...]:
        """Agent CLI args that pick up its last conversation, for a bottled
        agent resumed onto its kept home (it comes first, as Codex's is a
        subcommand). Default: none, a new conversation."""
        return ()

    def interactive_prompt(self, prompt: str) -> list[str] | None:
        """Agent CLI args that start an interactive session with `prompt` as
        its first message, or None if unsupported."""
        del prompt

    def transcript_glob(self, plan: "BottlePlan") -> str | None:
        """The guest path pattern (shell glob) of the harness's transcript
        files for `plan`, or None when it keeps none. Default: none."""
        del plan

    def parse_transcript(self, lines: Iterable[str], *, offset: int = 0,
                         seq: int = 0) -> list[TranscriptEntry]:
        """New raw transcript `lines` (each with its newline) as entries,
        numbered from `seq`, the first line at byte `offset` of the copy.
        Default: each line as a `system` entry."""
        out = []
        for line in lines:
            out.append(TranscriptEntry(seq=seq + len(out), ts="", kind="system",
                                       text=line.rstrip("\n"), raw_offset=offset))
            offset += len(line.encode("utf-8"))
        return out

    def active_entries(self, entries: list[TranscriptEntry]) -> list[TranscriptEntry]:
        """The entries on the branch the harness has active, for a harness
        whose transcript is a tree. Default: all of them."""
        return entries

    def working(self, lines: Iterable[str]) -> bool | None:
        """Whether the agent is working on a prompt, from the newest raw
        lines of its transcript copy (a tail; the first may be cut): True,
        False, or None when they don't say. None is the default, for a
        harness without turn markers: the host then counts it as working
        while its transcript grew within `transcript_serve.WORKING_FOR`.

        An indicator for the console only, never a security decision: the
        transcript is the agent's own output."""
        del lines

    def credential_refresh_spec(self, plan: "BottlePlan") -> CredentialRefreshSpec | None:
        """How to keep this bottle's forwarded host login current, or None (the
        default) when it forwards none."""
        del plan
        return None

    def host_login_paths(self, host_env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
        """The host files holding the login `refresh_credentials` reads, so
        the host controller can push a rotated login as soon as the host's
        CLI writes it, not only on its timer. None: the timer alone (a login
        kept outside a file, like the macOS Keychain, falls back to it too)."""
        del host_env
        return ()

    def refresh_credentials(self, spec: CredentialRefreshSpec, bottle: "Bottle",
                            host_env: Mapping[str, str] | None = None) -> CredentialRefresh:
        """Re-read the host login `spec` describes and bring the bottle's own
        copy up to date (a credential file the harness checks, say). The host
        controller pushes the values to the gateway. Raises HostLoginError when
        the host login is missing or expired."""
        raise NotImplementedError(f"{spec.harness} forwards no host login to refresh")

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
        identity_token = getattr(plan, "identity_token", "")
        if manifest_bottle.git or identity_token:
            from .control.git_gate_render import git_gate_render_gitconfig
            content = git_gate_render_gitconfig(
                manifest_bottle.git, plan.git_gate_insteadof_host,
                identity_token=identity_token,
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
            ) if manifest_bottle.git else None
            if configured is not None and configured.returncode != 0:
                die(
                    "git provisioning: the runtime user cannot read the "
                    f"git-gate insteadOf rules from {guest_gitconfig}: "
                    f"{(configured.stderr or configured.stdout).strip()}"
                )

        self.provision_commit_hooks(bottle, plan)

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

    def provision_commit_hooks(self, bottle: "Bottle", plan: "BottlePlan") -> None:
        """Install the git hooks that add the `Bot-Bottle` trailer (and the
        operator's `Co-Authored-By`) to every commit; the agent's env already
        points `core.hooksPath` at them (`git_gate.commit_hooks`). A failure
        warns: git-gate still refuses commits without the trailer."""
        from .contract.git_trailers import TRAILER_KEY
        from .git_gate import commit_hooks
        from .lib.log import info, warn

        trailer = plan.git_gate_plan.trailer
        operator_name, operator_email = commit_hooks.operator_identity()
        directory = commit_hooks.hooks_dir(plan.guest_home)
        dispatcher = plan.stage_dir / commit_hooks.DISPATCHER_NAME
        dispatcher.write_text(commit_hooks.render_dispatcher())
        config = plan.stage_dir / commit_hooks.CONFIG_NAME
        config.write_text(commit_hooks.render_hook_config(
            trailer, operator_name, operator_email))
        info(f"commit hooks in {directory}: {TRAILER_KEY}: {trailer}"
             + (f", co-author {operator_name} <{operator_email}>"
                if operator_name else ""))
        made = bottle.exec(f"mkdir -p {shlex.quote(directory)}", user="root")
        if made.returncode == 0:
            bottle.cp_in(str(dispatcher), f"{directory}/{commit_hooks.DISPATCHER_NAME}")
            bottle.cp_in(str(config), f"{directory}/{commit_hooks.CONFIG_NAME}")
            made = bottle.exec(commit_hooks.render_install(directory), user="root")
        if made.returncode != 0:
            warn("could not install the commit hooks; commits won't carry the "
                 f"{TRAILER_KEY} trailer: "
                 f"{(made.stderr or made.stdout).strip()}")


def _load_user_plugin(harness: str) -> AgentProvider | None:
    """A user-defined AgentProvider from ~/.bot-bottle/contrib/<harness>/
    (under `$BOT_BOTTLE_ROOT` when set), or
    None when there's no plugin directory. Raises ValueError if the file
    exports no subclass."""
    plugin_path = bot_bottle_root() / "contrib" / harness / "agent_provider.py"
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
