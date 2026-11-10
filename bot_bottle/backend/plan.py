"""Launch plan types every backend shares: the CLI's `BottleSpec` and the
`BottlePlan` a backend's prepare step resolves it into."""

from __future__ import annotations

import sys
from abc import ABC
from dataclasses import dataclass, field
from pathlib import Path

from ..agent_provider import AgentProvisionPlan, PromptMode
from ..contract.hostctl import Settings
from ..egress import EgressPlan
from ..git_gate import GitGatePlan
from ..lib.log import info, warn
from ..manifest import Manifest, ManifestIndex
from ..manifest.schema import DEFAULT_BOTTLE
from ..secret_source import MappingSecrets, SecretSource, tiers
from ..supervisor.plan import SupervisePlan
from ..workspace import WorkspacePlan, workspace_plan
from .print_util import print_multi, visible_agent_env_names


class EgressTokenSecrets(SecretSource):
    """Where egress token refs resolve: `provisioned` (the agent provider's
    values) first. A secret one of the bottle's own routes names then comes
    from where its `auth.value` says: `secret:NAME` from the secrets store
    (only with a warning from the launching environment), `secret:${VAR}`
    (`env_refs`) from the launching environment (only with a warning from the
    store). Any other (the provider's) from `secrets` as before (env, then
    store)."""

    def __init__(self, provisioned: SecretSource, secrets: SecretSource,
                 route_refs: frozenset[str], env_refs: frozenset[str] = frozenset()) -> None:
        self._provisioned = provisioned
        self._secrets = secrets
        self._route_refs = route_refs
        self._env_refs = env_refs

    def get(self, name: str) -> str | None:
        value = self._provisioned.get(name)
        if value is not None:
            return value
        if name not in self._route_refs and name not in self._env_refs:
            return self._secrets.get(name)
        env, store, _ = tiers(self._secrets)
        first, then = (env, store) if name in self._env_refs else (store, env)
        value = first.get(name)
        if value or env is store:
            return value
        value = then.get(name)
        if value and name in self._env_refs:
            warn(f"egress: route secret:${{{name}}} read {name} from the secrets file; "
                 f"write secret:{name} for that.")
        elif value:
            warn(f"egress: route token {name} came from the launching environment; "
                 f"secret:{name} reads the secrets file, so add {name} there.")
        return value

    def __repr__(self) -> str:
        return f"EgressTokenSecrets({self._secrets!r}, <{len(self._route_refs)} route refs>)"


@dataclass(frozen=True)
class BottleSpec:
    """CLI-supplied launch intent. Resolved values live on the plan."""

    manifest: ManifestIndex
    agent_name: str
    copy_cwd: bool
    user_cwd: str
    # Resume: reuse this identity instead of minting one. Empty for a fresh
    # start.
    identity: str = ""
    label: str = ""
    color: str = ""
    # Bottle names selected at launch, merged in order; they replace the
    # agent's `bottle:` when non-empty.
    bottle_names: tuple[str, ...] = ()
    # No TTY: host-key and secret checks fail instead of prompting. Every
    # host-controller launch.
    headless: bool = False
    # Repo name -> upstream host key the operator confirmed for this launch.
    host_keys: tuple[tuple[str, str], ...] = ()
    # With the bottle's `preserve_container`: start a new guest from the image
    # (its home kept) rather than the one kept from its last run.
    fresh_guest: bool = False
    # "fresh" builds as normal; "cached" reuses the local image or artifact.
    image_policy: str = "fresh"


@dataclass(frozen=True)
class BottlePlan(ABC):
    """Base output of a backend's prepare step; subclasses add backend fields."""

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
    # Values forwarded into the agent's env; kept out of the repr.
    forwarded_env: dict[str, str] = field(repr=False)
    # The agent's git-gate and supervise URLs (`http://<gateway>:<port>`), set
    # by launch.
    agent_git_gate_url: str = ""
    agent_supervise_url: str = ""
    # The identity token the agent presents on every attributed request, set by
    # launch from the registration.
    identity_token: str = ""
    # Key for the agent's stored egress secrets, delivered without touching
    # disk.
    env_var_secret: str = ""

    @property
    def container_name(self) -> str:
        return self.agent_provision.instance_name

    @property
    def image(self) -> str:
        return self.agent_provision.image

    @property
    def dockerfile_path(self) -> str:
        """The Dockerfile that builds `image`: the manifest's override, else
        the provider's."""
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
    def harness(self) -> str:
        return self.agent_provision.harness

    @property
    def git_gate_insteadof_host(self) -> str:
        """Host and port for git-gate insteadOf URLs, once launch has set them."""
        return self.agent_git_gate_url.removeprefix("http://").rstrip("/")

    @property
    def workspace_plan(self) -> WorkspacePlan:
        return workspace_plan(self.spec, guest_home=self.guest_home)

    @property
    def egress_secrets(self) -> SecretSource:
        """Where egress token_refs resolve (`EgressTokenSecrets`): the
        provider's provisioned values, then the store for the bottle's own
        routes, else `secrets`."""
        routes = self.manifest.bottle.egress.routes
        refs = frozenset(r.TokenRef for r in routes if r.TokenRef and not r.TokenFromEnv)
        env_refs = frozenset(r.TokenRef for r in routes if r.TokenRef and r.TokenFromEnv)
        return EgressTokenSecrets(
            MappingSecrets(self.agent_provision.provisioned_env), self.secrets, refs, env_refs)

    def print(self) -> None:
        """Print the y/N preflight summary to stderr."""
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
        info(f"provider        : {self.agent_provision.harness}")
        print_multi("env             ", env_names)
        print_multi("skills          ", list(agent.skills))
        effective_bottles = list(spec.bottle_names or (DEFAULT_BOTTLE,))
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
                auth = (f" [auth:{r.auth_scheme or r.auth_header}]"
                        if r.auth_scheme or r.auth_header else "")
                egress_lines.append(f"{r.host}{auth}")
            print_multi("  egress        ", egress_lines)
        print(file=sys.stderr)
