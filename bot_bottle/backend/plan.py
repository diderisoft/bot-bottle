"""The launch plan types every backend shares: the CLI's `BottleSpec` and the
`BottlePlan` a backend's prepare step resolves it into. Re-exported from
`backend.base`."""

from __future__ import annotations

import sys
from abc import ABC
from dataclasses import dataclass, field
from pathlib import Path

from ..agent_provider import AgentProvisionPlan, PromptMode
from ..contract.hostctl import Settings
from ..egress import EgressPlan
from ..git_gate import GitGatePlan
from ..lib.log import info
from ..manifest import Manifest, ManifestIndex
from ..secret_source import ChainSecrets, MappingSecrets, SecretSource
from ..supervisor.plan import SupervisePlan
from ..workspace import WorkspacePlan, workspace_plan
from .print_util import print_multi, visible_agent_env_names


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
    # Resume: prepare uses this identity instead of minting a fresh one, to
    # continue an existing bottle's state. Empty for a fresh `start`.
    identity: str = ""
    label: str = ""
    color: str = ""
    # Ordered bottle names selected at launch (issue #269). When non-empty
    # they are merged in order and replace the agent's `bottle:` field.
    bottle_names: tuple[str, ...] = ()
    # No TTY, no interactive prompts: the host-key preflight and secret
    # resolution error rather than prompt. Every host-controller launch.
    headless: bool = False
    # Repo name -> upstream host key the operator confirmed for this launch.
    host_keys: tuple[tuple[str, str], ...] = ()
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

    egress_plan: EgressPlan
    supervise_plan: SupervisePlan | None
    agent_provision: AgentProvisionPlan
    settings: Settings
    # Where launch resolves egress token_refs and forge tokens.
    secrets: SecretSource = field(repr=False)
    slug: str
    # Values forwarded into the agent's environment. repr=False keeps
    # secret, interpolated and OAuth values out of any log of the plan.
    forwarded_env: dict[str, str] = field(repr=False)
    # The agent's git-gate and supervise endpoints on the shared gateway
    # (`http://<gateway>:<port>`), stamped by launch. Empty until then.
    agent_git_gate_url: str = ""
    agent_supervise_url: str = ""
    # Per-bottle identity token the agent presents on every attributed request
    # (egress proxy credentials, git-gate/supervise headers); stamped by launch
    # from the orchestrator registration. Empty before registration.
    identity_token: str = ""
    # Encryption key for the agent's stored egress secrets. Each backend
    # delivers it without writing the value to disk.
    env_var_secret: str = ""

    @property
    def container_name(self) -> str:
        return self.agent_provision.instance_name

    @property
    def image(self) -> str:
        return self.agent_provision.image

    @property
    def dockerfile_path(self) -> str:
        """Absolute path to the Dockerfile that builds `image`: the manifest's
        per-bottle override, else the provider plugin's bundled Dockerfile."""
        return self.agent_provision.dockerfile

    @property
    def prompt_file(self) -> Path:
        return self.agent_provision.prompt_file

    @property
    def agent_command(self) -> str:
        return self.agent_provision.command

    @property
    def agent_prompt_mode(self) -> PromptMode:
        return self.agent_provision.prompt_mode

    @property
    def agent_provider_template(self) -> str:
        return self.agent_provision.template

    @property
    def git_gate_insteadof_host(self) -> str:
        """Host and port used in git-gate insteadOf URLs: the shared
        gateway's git-http endpoint once launch has stamped it."""
        if self.agent_git_gate_url.startswith("http://"):
            return self.agent_git_gate_url.removeprefix("http://").rstrip("/")
        return "git-gate"

    @property
    def git_gate_insteadof_scheme(self) -> str:
        """URL scheme for git-gate insteadOf rewrites."""
        if self.agent_git_gate_url.startswith("http://"):
            return "http"
        return "git"

    @property
    def workspace_plan(self) -> WorkspacePlan:
        return workspace_plan(self.spec, guest_home=self.guest_home)

    @property
    def egress_secrets(self) -> SecretSource:
        """Where egress token_refs resolve: the provider's provisioned
        values (host credentials it read), then `secrets`."""
        return ChainSecrets(
            MappingSecrets(self.agent_provision.provisioned_env), self.secrets)

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
