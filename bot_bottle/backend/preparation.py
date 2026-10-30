"""Shared preparation: turn a `BottleSpec` into the inputs a backend's
`_resolve_plan` takes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ..agent_provider import AgentProvisionPlan, build_agent_provision_plan, get_provider
from ..egress import EgressPlan
from ..env import ResolvedEnv, resolve_env
from ..git_gate import GitGate, GitGatePlan
from ..git_gate.host_key import with_host_keys
from ..manifest import Manifest
from ..secret_source import SecretSource
from ..supervisor.plan import SupervisePlan
from ..workspace import workspace_plan
from .resolve_common import (
    launch_trailer,
    merge_provision_env_vars,
    mint_slug,
    prepare_agent_state_dir,
    prepare_egress,
    prepare_git_gate,
    prepare_supervise,
    reject_nested_containers,
    resolve_manifest_dockerfile,
    with_commit_hooks_env,
    write_launch_metadata,
)

if TYPE_CHECKING:
    from .base import BottleSpec


def _egress_owner(spec: "BottleSpec", manifest: Manifest) -> str:
    """The agent and its bottle(s), for a launch error."""
    bottles = ", ".join(spec.bottle_names) or manifest.agent.bottle
    return f"agent '{spec.agent_name}' with bottle {bottles}"


class PreparationBackend(Protocol):
    """Backend hooks the shared planner calls."""

    name: str
    supports_nested_containers: bool

    def _validate(self, spec: BottleSpec) -> Manifest: ...
    def _preflight(self) -> None: ...
    def _build_guest_env(self, resolved_env: ResolvedEnv) -> dict[str, str]: ...


@dataclass(frozen=True)
class PreparedBottle:
    """Backend-neutral result of shared preparation."""

    manifest: Manifest
    slug: str
    resolved_env: ResolvedEnv
    agent_provision_plan: AgentProvisionPlan
    egress_plan: EgressPlan
    git_gate_plan: GitGatePlan
    supervise_plan: SupervisePlan | None


class BottlePreparationPlanner:
    """Run the shared, side-effect-limited part of preparation."""

    def __init__(self, backend: PreparationBackend) -> None:
        self._backend = backend

    def prepare(self, spec: BottleSpec, secrets: SecretSource) -> PreparedBottle:
        backend = self._backend
        # Protected on purpose: only this planner calls these hooks.
        manifest = backend._validate(spec)  # pylint: disable=protected-access
        if not backend.supports_nested_containers:
            reject_nested_containers(backend.name, manifest)

        backend._preflight()  # pylint: disable=protected-access
        manifest = GitGate().preflight_host_keys(
            with_host_keys(manifest, dict(spec.host_keys)),
            headless=spec.headless,
            home_md=spec.manifest.home_md,
        )

        bottle = manifest.bottle
        provider_config = manifest.agent.agent_provider
        provider = get_provider(provider_config.harness)
        resolved_env = resolve_env(manifest, secrets, prompt=not spec.headless)
        workspace = workspace_plan(spec, guest_home=provider.guest_home)
        slug = mint_slug(spec)
        write_launch_metadata(slug, spec, compose_project="", backend=backend.name)

        dockerfile = (
            resolve_manifest_dockerfile(provider_config.dockerfile, spec)
            if provider_config.dockerfile
            else str(provider.dockerfile)
        )
        agent_dir, prompt_file = prepare_agent_state_dir(slug, manifest)
        provision = build_agent_provision_plan(
            harness=provider_config.harness,
            dockerfile=dockerfile,
            state_dir=agent_dir,
            instance_name=f"bot-bottle-{slug}",
            prompt_file=prompt_file,
            guest_env=backend._build_guest_env(  # pylint: disable=protected-access
                resolved_env
            ),
            forward_host_credentials=provider_config.forward_host_credentials,
            auth_token=provider_config.auth_token,
            trusted_project_path=workspace.workdir,
            label=spec.label,
            color=spec.color,
            provider_settings=provider_config.settings,
        )
        provision = with_commit_hooks_env(merge_provision_env_vars(provision))
        return PreparedBottle(
            manifest=manifest,
            slug=slug,
            resolved_env=resolved_env,
            agent_provision_plan=provision,
            egress_plan=prepare_egress(
                bottle, slug, provision, owner=_egress_owner(spec, manifest)),
            git_gate_plan=prepare_git_gate(
                bottle, slug, launch_trailer(spec, manifest, slug)),
            supervise_plan=prepare_supervise(bottle, slug),
        )
