"""Prepare step for the Docker bottle backend.

`resolve_plan` does all host-side resolution (image and container
names, env-file, prompt-file, proxy plan, runtime detection) and
returns a frozen DockerBottlePlan. No Docker resources are created;
the only side effects are scratch files under `stage_dir` and a probe
of `docker info`. Cross-backend host-side validation has already run
via the base class's `prepare` template before this is called.
"""

from __future__ import annotations

import os
from pathlib import Path

from ...agent_provider import PROVIDER_TEMPLATES, agent_provision_plan, get_provider
from ...env import ResolvedEnv, resolve_env
from ...log import die
from ...workspace import workspace_plan as resolve_workspace_plan
from .. import BottleSpec
from ..resolve_common import (
    merge_provision_env_vars,
    mint_slug,
    prepare_agent_state_dir,
    prepare_egress,
    prepare_git_gate,
    prepare_supervise,
    resolve_manifest_dockerfile,
    write_launch_metadata,
)
from . import util as docker_mod
from .bottle_plan import DockerBottlePlan
from ...bottle_state import (
    clear_preserve_marker,
    per_bottle_dockerfile,
    per_bottle_dockerfile_path,
    per_bottle_image_tag,
)
from .sidecar_bundle import sidecar_bundle_container_name


def resolve_plan(
    spec: BottleSpec,
    *,
    stage_dir: Path,
) -> DockerBottlePlan:
    """Resolve Docker-specific names and write scratch files. Trusts
    that the agent and its skills/git-gate keys are present —
    validation already ran in the base class."""
    docker_mod.require_docker()

    manifest = spec.manifest
    bottle = manifest.bottle_for(spec.agent_name)
    provider = bottle.agent_provider
    provider_obj = get_provider(provider.template)
    provider_runtime = provider_obj.runtime
    guest_home = "/home/node"
    workspace_plan = resolve_workspace_plan(spec, guest_home=guest_home)

    slug = mint_slug(spec)
    write_launch_metadata(slug, spec, compose_project=f"bot-bottle-{slug}", backend="docker")
    # Clear any leftover preserve marker from a prior capability-block
    # so this fresh launch can be cleaned up at session-end unless
    # the agent triggers another capability-block.
    clear_preserve_marker(slug)

    # PRD 0016 capability-block: if a per-bottle Dockerfile has been
    # written (via apply_capability_change), the base image becomes
    # per_bottle_image_tag(slug) built from that file. --cwd still
    # layers a derived image on top.
    if provider.template in PROVIDER_TEMPLATES:
        image = provider_runtime.image
    else:
        image = f"bot-bottle-{provider.template}:{slug}"
    dockerfile_path = str(provider_obj.dockerfile)
    if per_bottle_dockerfile(slug) is not None:
        image = per_bottle_image_tag(slug)
        dockerfile_path = str(per_bottle_dockerfile_path(slug))
    elif provider.dockerfile:
        image = f"bot-bottle-{provider.template}:{slug}"
        dockerfile_path = resolve_manifest_dockerfile(provider.dockerfile, spec)
    derived_image = ""
    runtime_image = image
    if spec.copy_cwd:
        derived_image = os.environ.get(
            "BOT_BOTTLE_DERIVED_IMAGE", f"bot-bottle-cwd:{slug}"
        )
        runtime_image = derived_image

    default_container = f"bot-bottle-{slug}"
    pinned_container = os.environ.get("BOT_BOTTLE_CONTAINER", "")
    container_name_pinned = bool(pinned_container)
    if container_name_pinned:
        container_name = pinned_container
        if docker_mod.container_exists(container_name):
            die(
                f"container '{container_name}' already exists "
                f"(pinned via BOT_BOTTLE_CONTAINER). "
                f"Remove it with 'docker rm -f {container_name}' or unset the override."
            )
    else:
        container_name = ""
        for candidate in docker_mod.container_name_candidates(default_container):
            if not docker_mod.container_exists(candidate):
                container_name = candidate
                break
        if not container_name:
            die(
                f"could not find a free container name after "
                f"{default_container}-{docker_mod.MAX_CONTAINER_SUFFIX}; "
                f"clean up old containers with 'docker rm -f <name>'"
            )

    # Probe the sidecar-bundle container name for an orphan from a
    # previous run. Otherwise a stale bundle surfaces as a
    # docker-create conflict deep inside launch() with no actionable
    # hint; failing fast here points at the cleanup command.
    bundle_name = sidecar_bundle_container_name(slug)
    if docker_mod.container_exists(bundle_name):
        die(
            f"sidecar bundle container '{bundle_name}' already exists. "
            f"This is an orphan from a previous run; clean it up with "
            f"'./cli.py cleanup' (or 'docker rm -f {bundle_name}') and "
            f"retry."
        )

    agent_dir, prompt_file = prepare_agent_state_dir(slug, spec)
    env_file = agent_dir / "agent.env"

    git_gate_plan = prepare_git_gate(bottle, slug)

    resolved = resolve_env(manifest, spec.agent_name)
    forwarded_env: dict[str, str] = dict(resolved.forwarded)
    _write_env_file(resolved, env_file)

    use_runsc = docker_mod.runsc_available()
    agent_provision = agent_provision_plan(
        template=provider.template,
        dockerfile=dockerfile_path,
        state_dir=agent_dir,
        guest_home=guest_home,
        forward_host_credentials=provider.forward_host_credentials,
        auth_token=provider.auth_token,
        host_env=dict(os.environ),
        trusted_project_path=workspace_plan.workdir,
        label=spec.label,
        color=spec.color,
    )
    agent_provision = merge_provision_env_vars(agent_provision)

    egress_plan = prepare_egress(bottle, slug, agent_provision)

    # Current Dockerfile for the agent image. For `--cwd` derived
    # images the base Dockerfile is what the agent should propose
    # changes against (the derived layer is just a workspace copy).
    # (routes.yaml used to land here too but PRD 0017 chunk 3
    # moved it behind the `list-egress-routes` MCP tool so the
    # agent gets live state rather than a launch-time snapshot.)
    supervise_dockerfile_path = (
        Path(dockerfile_path) if dockerfile_path else provider_obj.dockerfile
    )
    dockerfile_content = (
        supervise_dockerfile_path.read_text(encoding="utf-8")
        if supervise_dockerfile_path.is_file()
        else ""
    )
    supervise_plan = prepare_supervise(bottle, slug, dockerfile_content=dockerfile_content)

    return DockerBottlePlan(
        spec=spec,
        stage_dir=stage_dir,
        slug=slug,
        container_name=container_name,
        container_name_pinned=container_name_pinned,
        image=image,
        derived_image=derived_image,
        runtime_image=runtime_image,
        dockerfile_path=dockerfile_path,
        env_file=env_file,
        forwarded_env=forwarded_env,
        prompt_file=prompt_file,
        git_gate_plan=git_gate_plan,
        egress_plan=egress_plan,
        supervise_plan=supervise_plan,
        use_runsc=use_runsc,
        agent_provision=agent_provision,
        workspace_plan=workspace_plan,
    )


def _write_env_file(resolved: ResolvedEnv, env_file: Path) -> None:
    """Serialize the literal portion of a ResolvedEnv into docker's
    `--env-file` syntax (NAME=VALUE per line, mode 600 since the file
    may carry verbatim values from the manifest). Forwarded names ride
    on the plan as a structured tuple instead."""
    env_lines: list[str] = []
    for name, value in resolved.literals.items():
        if "\n" in value:
            die(
                f"env entry {name} (literal) contains a newline; "
                f"docker --env-file cannot represent multi-line values."
            )
        env_lines.append(f"{name}={value}")
    env_file.write_text("\n".join(env_lines) + ("\n" if env_lines else ""))
    env_file.chmod(0o600)


