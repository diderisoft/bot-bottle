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
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path

from ...agent_provider import PROVIDER_TEMPLATES, agent_provision_plan, runtime_for
from ...egress import Egress
from ...env import ResolvedEnv, resolve_env
from ...git_gate import GitGate
from ...log import die
from ...supervise import Supervise
from ...workspace import workspace_plan as resolve_workspace_plan
from .. import BottleSpec
from . import util as docker_mod
from .bottle_plan import DockerBottlePlan
from .bottle_state import (
    BottleMetadata,
    agent_state_dir,
    bottle_identity,
    clear_preserve_marker,
    egress_state_dir,
    git_gate_state_dir,
    per_bottle_dockerfile,
    per_bottle_dockerfile_path,
    per_bottle_image_tag,
    supervise_state_dir,
    write_metadata,
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

    git_gate = GitGate()
    egress = Egress()
    supervise = Supervise()

    manifest = spec.manifest
    agent = manifest.agents[spec.agent_name]
    bottle = manifest.bottle_for(spec.agent_name)
    provider = bottle.agent_provider
    provider_runtime = runtime_for(provider.template)
    guest_home = "/home/node"
    workspace_plan = resolve_workspace_plan(spec, guest_home=guest_home)

    # PRD 0016 follow-up: identity, not bare slug. A fresh `start`
    # mints a random-suffixed identity (so parallel runs of the same
    # agent in the same cwd don't collide on container/network
    # names); a `resume` passes the recorded identity in via
    # spec.identity to continue an existing bottle's state.
    slug = spec.identity or bottle_identity(spec.agent_name)
    # Record the launch metadata so `cli.py resume <identity>` can
    # reconstruct the spec. Idempotent — re-writes on resume with a
    # refreshed started_at.
    write_metadata(BottleMetadata(
        identity=slug,
        agent_name=spec.agent_name,
        cwd=spec.user_cwd if spec.copy_cwd else "",
        copy_cwd=spec.copy_cwd,
        started_at=datetime.now(timezone.utc).isoformat(),
        compose_project=f"bot-bottle-{slug}",
        backend="docker",
        label=spec.label,
        color=spec.color,
    ))
    # Clear any leftover preserve marker from a prior capability-block
    # so this fresh launch can be cleaned up at session-end unless
    # the agent triggers another capability-block.
    clear_preserve_marker(slug)

    # PRD 0016 capability-block: if a per-bottle Dockerfile has been
    # written (via apply_capability_change), the base image becomes
    # per_bottle_image_tag(slug) built from that file. --cwd still
    # layers a derived image on top.
    dockerfile_path = ""
    if per_bottle_dockerfile(slug) is not None:
        image_default = per_bottle_image_tag(slug)
        dockerfile_path = str(per_bottle_dockerfile_path(slug))
    elif provider.dockerfile:
        image_default = f"bot-bottle-{provider.template}:{slug}"
        dockerfile_path = _resolve_manifest_dockerfile(provider.dockerfile, spec)
    elif provider_runtime.dockerfile:
        image_default = provider_runtime.image
        dockerfile_path = provider_runtime.dockerfile
    elif provider.template not in PROVIDER_TEMPLATES:
        user_dockerfile = (
            Path.home() / ".bot-bottle" / "contrib" / provider.template / "Dockerfile"
        )
        if user_dockerfile.is_file():
            image_default = f"bot-bottle-{provider.template}:{slug}"
            dockerfile_path = str(user_dockerfile)
        else:
            image_default = provider_runtime.image
    else:
        image_default = provider_runtime.image
    image = os.environ.get("BOT_BOTTLE_IMAGE", image_default)
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

    # PRD 0018 chunk 2: prepare-time scratch files live under
    # ~/.bot-bottle/state/<slug>/<service>/ so chunk 3's compose
    # bind-mounts can point at stable paths. The state subdirs are
    # cleaned up by start.py's session-end teardown unless something
    # explicitly preserves the state dir (capability-block, crash).
    agent_dir = agent_state_dir(slug)
    agent_dir.mkdir(parents=True, exist_ok=True)
    env_file = agent_dir / "agent.env"
    prompt_file = agent_dir / "prompt.txt"
    prompt_file.write_text("")
    prompt_file.chmod(0o600)

    git_gate_dir = git_gate_state_dir(slug)
    git_gate_dir.mkdir(parents=True, exist_ok=True)
    git_gate_plan = git_gate.prepare(bottle, slug, git_gate_dir)

    resolved = resolve_env(manifest, spec.agent_name)
    # Everything that should reach the bottle by-name (so its value
    # never lands on argv or in env_file) goes into one dict. Nothing
    # mutates the host os.environ.
    forwarded_env: dict[str, str] = dict(resolved.forwarded)
    _write_env_file(resolved, env_file)
    prompt_file.write_text(agent.prompt)

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
    guest_env = dict(agent_provision.guest_env)
    for key, val in agent_provision.env_vars.items():
        guest_env.setdefault(key, val)
    agent_provision = replace(agent_provision, guest_env=guest_env)

    egress_dir = egress_state_dir(slug)
    egress_dir.mkdir(parents=True, exist_ok=True)
    egress_plan = egress.prepare(
        bottle, slug, egress_dir, agent_provision.egress_routes,
    )

    supervise_plan = None
    if bottle.supervise:
        # Current Dockerfile for the agent image. Read from the repo
        # root; for `--cwd` derived images the base Dockerfile is what
        # the agent should propose changes against (the derived layer
        # is just a workspace copy).
        # (routes.yaml used to land here too but PRD 0017 chunk 3
        # moved it behind the `list-egress-routes` MCP tool so the
        # agent gets live state rather than a launch-time snapshot.)
        supervise_dockerfile_path = (
            Path(dockerfile_path)
            if dockerfile_path
            else Path(__file__).resolve().parent.parent.parent.parent / "Dockerfile.claude"
        )
        dockerfile_content = (
            supervise_dockerfile_path.read_text(encoding="utf-8")
            if supervise_dockerfile_path.is_file()
            else ""
        )
        supervise_dir = supervise_state_dir(slug)
        supervise_dir.mkdir(parents=True, exist_ok=True)
        supervise_plan = supervise.prepare(
            slug, supervise_dir,
            dockerfile_content=dockerfile_content,
        )

    return DockerBottlePlan(
        spec=spec,
        stage_dir=stage_dir,
        guest_home=guest_home,
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


def _resolve_manifest_dockerfile(path_value: str, spec: BottleSpec) -> str:
    path = Path(os.path.expanduser(path_value))
    if not path.is_absolute():
        path = Path(spec.user_cwd) / path
    return str(path)
