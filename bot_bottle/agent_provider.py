"""Agent provider runtime mapping.

The manifest owns the user-facing AgentProvider shape. This module is
the launch-time table that turns a provider template into an executable
command, default image, and prompt/auth behavior.

Per PRD 0050 the per-provider implementations live under
`bot_bottle/contrib/<template>/agent_provider.py`. This module exposes:

  - `AgentProvider` (ABC) — the contract each plugin implements.
  - `get_provider(template)` — lazy-imported registry; the analogue
    of `bot_bottle/deploy_key_provisioner.get_provisioner`.
  - `AgentProvisionPlan` (+ helper dataclasses) — declarative shape
    each provider produces and the backends consume unchanged.
  - `agent_provision_plan` / `runtime_for` — thin wrappers around the
    registry kept so existing callers keep working without per-call
    edits.
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
PROVIDER_TEMPLATES = frozenset({PROVIDER_CLAUDE, PROVIDER_CODEX, PROVIDER_PI})

# Hosts that egress injects the host ChatGPT bearer on when Codex
# forward_host_credentials is enabled. Pipelock must pass these through
# (no TLS MITM) or its header DLP blocks the injected JWT.
CODEX_HOST_CREDENTIAL_HOSTS = ("api.openai.com", "chatgpt.com")

# Host that egress injects the host Claude bearer on when Claude
# forward_host_credentials is enabled.
CLAUDE_HOST_CREDENTIAL_HOSTS = ("api.anthropic.com",)
PromptMode = Literal[
    "append_file",
    "read_prompt_file",
    "print_read_prompt_file",
    "append_system_prompt",
]


@dataclass(frozen=True)
class AgentProviderRuntime:
    template: str
    command: str
    image: str
    prompt_mode: PromptMode
    bypass_args: tuple[str, ...]
    resume_args: tuple[str, ...]
    # argv run inside a throwaway container of a freshly built agent
    # image, right after `build_image()`, to catch a build that
    # exited 0 but produced a broken CLI (e.g. an npm
    # optionalDependencies fetch for a platform-native binary that
    # silently no-ops on a transient failure). Empty tuple skips the
    # check — not every provider has opted in yet.
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
    """Provider-owned guest setup.

    Backends interpret this plan with their own copy/exec primitives.
    Provider-specific content stays here so future provider plugins can
    return the same shape without adding backend-plan fields.

    `egress_routes` are provider-declared EgressRoutes that backends
    pass to `Egress.prepare`. This keeps provider logic out of the
    egress module — it merges provider routes generically without
    knowing the provider type.

    `hidden_env_names` is the set of env var names the provider injected
    as non-secret placeholders. `print_util.visible_agent_env_names` uses
    this to suppress them from the preflight summary so operators don't
    mistake them for real credentials.
    """

    template: str
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


class AgentProvider(ABC):
    """Per-template plugin: produces the provision plan and applies
    the provider-specific in-guest setup steps (skills, prompt, the
    declarative `dirs`/`files`/`pre_copy`/`verify` apply loop, and
    supervise MCP registration). Concrete subclasses live under
    `bot_bottle/contrib/<template>/agent_provider.py`."""

    @property
    @abstractmethod
    def runtime(self) -> AgentProviderRuntime:
        """The static command / image / prompt-mode table for this
        template."""

    @property
    def guest_home(self) -> str:
        """In-guest home directory for the agent user. Defaults to
        `/home/node` to match the Debian-based bot-bottle-* images
        (USER node). Override for plugins whose image runs as a
        different user."""
        return "/home/node"

    @property
    def image_user(self) -> str:
        """The user the default image ends as (its final `USER`). An overlay
        layer switches to root for its RUN steps and restores this afterwards.
        Defaults to `node` to match the Debian-based bot-bottle-* images."""
        return "node"

    def harness_overlay_lines(self, version: str) -> tuple[str, ...] | None:
        """Dockerfile lines that replace this provider's pinned harness with
        `version` in an overlay layer (PRD 0085), or None when the provider
        does not support a harness bump. The lines start as root and may
        switch user; the overlay restores `image_user` afterwards. They must
        fail the build unless the installed harness reports exactly
        `version`. `version` is already validated as semver-shaped."""
        return None

    @property
    def dockerfile(self) -> Path:
        """Path to the provider's Dockerfile.

        Default: the `Dockerfile` file next to this provider's
        `agent_provider.py` module. Override to point at a non-standard
        path."""
        return Path(inspect.getfile(type(self))).parent / "Dockerfile"

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
        """Build the declarative AgentProvisionPlan for one launch.
        Backends call this during `prepare` and consume the result as
        before.

        `auth_token` is a secret's name (an egress token_ref), never its
        value. `host_env` only locates the host's credential files (`HOME`,
        `CODEX_HOME`); None means this process's environment."""

    @abstractmethod
    def provision_skills(self, plan: "BottlePlan", bottle: "Bottle") -> None:
        """Copy each of the agent's named skills from the host into
        the guest. No-op when the agent has no skills. The in-guest
        layout is provider-specific (claude-code's
        `~/.claude/skills/` today; future providers may differ)."""

    @abstractmethod
    def provision_prompt(self, plan: "BottlePlan", bottle: "Bottle") -> str | None:
        """Copy the prompt file into the guest, fix ownership/mode,
        and return the in-guest path iff the agent has a non-empty
        prompt (drives the `--append-system-prompt-file` flag).

        The file is copied either way so the path always exists."""

    @abstractmethod
    def provision(self, plan: "BottlePlan", bottle: "Bottle") -> None:
        """Apply the provider's declarative
        `dirs`/`pre_copy`/`files`/`verify` steps from
        `plan.agent_provision`. Was called `provision_provider_auth`
        on `BottleBackend` before PRD 0050."""

    @abstractmethod
    def provision_supervise_mcp(
        self,
        plan: "BottlePlan",
        bottle: "Bottle",
        supervise_url: str,
    ) -> None:
        """Register the per-bottle supervise daemon as an MCP server
        in the provider's in-guest config. Called by the backend after
        the supervise daemon is reachable. No-op when
        `plan.supervise_plan is None`."""

    @abstractmethod
    def headless_prompt(self, prompt: str) -> list[str]:
        """Return the agent CLI args that deliver `prompt` as the
        initial task in a non-interactive (headless) session.

        Called only when ``--prompt`` is passed to
        ``bot-bottle start --headless``; the returned args are appended
        after the provider's ``bypass_args`` and ``startup_args``."""

    def provision_ca(self, bottle: "Bottle", plan: "BottlePlan") -> None:
        """Install the egress MITM CA into the agent's trust store.

        Default: Debian-style — cp the cert to the standard source path,
        run update-ca-certificates, log the fingerprint. Override for
        non-Debian base images or non-standard trust mechanisms."""
        from .backend.util import AGENT_CA_PATH, log_ca_fingerprint, select_ca_cert
        from .lib.log import die
        cert_host_path, label = select_ca_cert(plan.egress_plan)
        # Ensure the target directory exists. A backend's rootfs build
        # may not preserve the empty /usr/local/share/ca-certificates/
        # directory; mkdir -p is idempotent and safe for all backends.
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
        """Configure git inside the agent container.

        Default: Debian/node — writes the git-gate insteadOf gitconfig
        and sets user.name/email as node. Workspace copy runs through
        BottleBackend.provision_workspace against the running bottle."""
        from .lib.log import die, info

        # Firecracker exports image rootfs files through an unprivileged host
        # tar extraction, so image-time ownership of XDG directories is not
        # preserved.  Git consults ~/.config/git even when the actual config
        # is ~/.gitconfig; an unreadable directory there can prevent the
        # git-gate insteadOf rules below from taking effect.  Repair this at
        # runtime, after every backend's copy/export path has completed.
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
            from .git_gate import GIT_GATE_HOSTNAME, git_gate_render_gitconfig
            gate_host = getattr(plan, "git_gate_insteadof_host", GIT_GATE_HOSTNAME)
            gate_scheme = getattr(plan, "git_gate_insteadof_scheme", "git")
            content = git_gate_render_gitconfig(
                manifest_bottle.git, gate_host, scheme=gate_scheme,
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


def _load_user_plugin(template: str) -> AgentProvider | None:
    """Check ~/.bot-bottle/contrib/<template>/agent_provider.py for a
    user-defined AgentProvider subclass. Returns an instance if found,
    None if the plugin directory doesn't exist, raises ValueError if
    the file exists but exports no AgentProvider subclass."""
    plugin_path = (
        Path.home() / ".bot-bottle" / "contrib" / template / "agent_provider.py"
    )
    if not plugin_path.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        f"_user_contrib_{template}.agent_provider", plugin_path
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


def get_provider(template: str) -> AgentProvider:
    """Resolve a provider template name to its plugin instance.

    Checks ~/.bot-bottle/contrib/<template>/agent_provider.py first so
    users can shadow a built-in for local testing. Falls through to the
    built-in registry; raises ValueError for unknown names with no
    matching user plugin."""
    user_plugin = _load_user_plugin(template)
    if user_plugin is not None:
        return user_plugin
    if template == PROVIDER_CLAUDE:
        from .contrib.claude.agent_provider import ClaudeAgentProvider
        return ClaudeAgentProvider()
    if template == PROVIDER_CODEX:
        from .contrib.codex.agent_provider import CodexAgentProvider
        return CodexAgentProvider()
    if template == PROVIDER_PI:
        from .contrib.pi.agent_provider import PiAgentProvider
        return PiAgentProvider()
    raise ValueError(f"unknown agent provider template: {template!r}")


def runtime_for(template: str) -> AgentProviderRuntime:
    return get_provider(template).runtime


def build_agent_provision_plan(
    *,
    template: str,
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
    """Back-compat shim — `prepare` callers stay the same; the work
    now lives on the provider plugin."""
    return get_provider(template).provision_plan(
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
