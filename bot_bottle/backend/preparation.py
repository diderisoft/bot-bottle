"""Backend-neutral preparation planner.

This module owns the shared transformation from a CLI ``BottleSpec`` to the
typed inputs consumed by a concrete backend's ``_resolve_plan``. Backend
classes retain only their validation/preflight/env hooks and their
backend-specific final resolution.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ..agent_provider import AgentProvisionPlan, build_agent_provision_plan, get_provider
from ..egress import EgressPlan
from ..env import ResolvedEnv, resolve_env
from ..git_gate import GitGate, GitGatePlan
from ..manifest import Manifest
from ..supervisor.plan import SupervisePlan
from ..workspace import workspace_plan
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

if TYPE_CHECKING:
    from .base import BottleSpec


class PreparationBackend(Protocol):
    """Backend hooks needed by the shared planner."""

    name: str
    supports_nested_containers: bool

    def _validate(self, spec: BottleSpec) -> Manifest: ...
    def _preflight(self) -> None: ...
    def _build_guest_env(self, resolved_env: ResolvedEnv) -> dict[str, str]: ...


@dataclass(frozen=True)
class PreparedBottle:
    """Typed, backend-neutral result of shared launch preparation."""

    manifest: Manifest
    slug: str
    resolved_env: ResolvedEnv
    agent_provision_plan: AgentProvisionPlan
    egress_plan: EgressPlan
    git_gate_plan: GitGatePlan
    supervise_plan: SupervisePlan | None


class BottlePreparationPlanner:
    """Run the common, side-effect-limited part of bottle preparation."""

    def __init__(self, backend: PreparationBackend) -> None:
        self._backend = backend

    def prepare(self, spec: BottleSpec) -> PreparedBottle:
        backend = self._backend
        # These are deliberately protected backend hooks: only this shared
        # planner orchestrates them, while concrete backends provide the
        # implementation.
        manifest = backend._validate(spec)  # pylint: disable=protected-access
        if not backend.supports_nested_containers:
            reject_nested_containers(backend.name, manifest)

        backend._preflight()  # pylint: disable=protected-access
        manifest = GitGate().preflight_host_keys(
            manifest,
            headless=spec.headless,
            home_md=spec.manifest.home_md,
        )

        bottle = manifest.bottle
        provider_config = bottle.agent_provider
        provider = get_provider(provider_config.template)
        resolved_env = resolve_env(manifest)
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
            template=provider_config.template,
            dockerfile=dockerfile,
            state_dir=agent_dir,
            instance_name=f"bot-bottle-{slug}",
            prompt_file=prompt_file,
            guest_env=backend._build_guest_env(  # pylint: disable=protected-access
                resolved_env
            ),
            forward_host_credentials=provider_config.forward_host_credentials,
            auth_token=provider_config.auth_token,
            host_env=dict(os.environ),
            trusted_project_path=workspace.workdir,
            label=spec.label,
            color=spec.color,
            provider_settings=provider_config.settings,
        )
        provision = merge_provision_env_vars(provision)
        return PreparedBottle(
            manifest=manifest,
            slug=slug,
            resolved_env=resolved_env,
            agent_provision_plan=provision,
            egress_plan=prepare_egress(bottle, slug, provision),
            git_gate_plan=prepare_git_gate(bottle, slug),
            supervise_plan=prepare_supervise(bottle, slug),
        )
