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
from pathlib import Path

from ... import pipelock
from ...egress_proxy import egress_proxy_render_routes
from ...env import ResolvedEnv, resolve_env
from ...log import die
from .. import BottleSpec
from . import util as docker_mod
from .bottle_plan import DockerBottlePlan
from .egress_proxy import DockerEgressProxy, egress_proxy_container_name
from .git_gate import DockerGitGate, git_gate_container_name
from .bottle_state import (
    BottleMetadata,
    bottle_identity,
    clear_preserve_marker,
    per_bottle_dockerfile,
    per_bottle_dockerfile_path,
    per_bottle_image_tag,
    write_metadata,
)
from .pipelock import DockerPipelockProxy, pipelock_container_name
from .supervise import DockerSupervise, supervise_container_name


def resolve_plan(
    spec: BottleSpec,
    *,
    stage_dir: Path,
    proxy: DockerPipelockProxy,
    git_gate: DockerGitGate,
    egress_proxy: DockerEgressProxy,
    supervise: DockerSupervise,
) -> DockerBottlePlan:
    """Resolve Docker-specific names and write scratch files. Trusts
    that the agent and its skills/git-gate keys are present —
    validation already ran in the base class."""
    docker_mod.require_docker()

    manifest = spec.manifest
    agent = manifest.agents[spec.agent_name]
    bottle = manifest.bottle_for(spec.agent_name)

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
    else:
        image_default = "claude-bottle:latest"
    image = os.environ.get("CLAUDE_BOTTLE_IMAGE", image_default)
    derived_image = ""
    runtime_image = image
    if spec.copy_cwd:
        derived_image = os.environ.get(
            "CLAUDE_BOTTLE_DERIVED_IMAGE", f"claude-bottle:cwd-{slug}"
        )
        runtime_image = derived_image

    default_container = f"claude-bottle-{slug}"
    pinned_container = os.environ.get("CLAUDE_BOTTLE_CONTAINER", "")
    container_name_pinned = bool(pinned_container)
    if container_name_pinned:
        container_name = pinned_container
        if docker_mod.container_exists(container_name):
            die(
                f"container '{container_name}' already exists "
                f"(pinned via CLAUDE_BOTTLE_CONTAINER). "
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

    # Probe sidecar container names for orphans from a previous run.
    # Sidecar names are deterministic from the slug; an orphan would
    # surface as a docker-create conflict deep inside launch() with no
    # actionable hint. Fail fast here with a cleanup pointer instead.
    # Only probe sidecars this launch will actually try to create:
    # pipelock always; git-gate when bottle.git is non-empty;
    # egress-proxy when bottle.egress_proxy.routes is non-empty.
    sidecar_probes: list[tuple[str, str]] = [
        ("pipelock", pipelock_container_name(slug)),
    ]
    if bottle.git:
        sidecar_probes.append(("git-gate", git_gate_container_name(slug)))
    if bottle.egress_proxy.routes:
        sidecar_probes.append(("egress-proxy", egress_proxy_container_name(slug)))
    if bottle.supervise:
        sidecar_probes.append(("supervise", supervise_container_name(slug)))
    for label, sidecar_name in sidecar_probes:
        if docker_mod.container_exists(sidecar_name):
            die(
                f"{label} sidecar container '{sidecar_name}' already exists. "
                f"This is an orphan from a previous run; clean it up with "
                f"'./cli.py cleanup' (or 'docker rm -f {sidecar_name}') and "
                f"retry."
            )

    env_file = stage_dir / "agent.env"
    prompt_file = stage_dir / "prompt.txt"
    prompt_file.write_text("")
    prompt_file.chmod(0o600)

    proxy_plan = proxy.prepare(bottle, slug, stage_dir)
    git_gate_plan = git_gate.prepare(bottle, slug, stage_dir)
    egress_proxy_plan = egress_proxy.prepare(bottle, slug, stage_dir)
    supervise_plan = None
    if bottle.supervise:
        routes_content = (
            egress_proxy_render_routes(egress_proxy_plan.routes)
            if egress_proxy_plan.routes else ""
        )
        allowlist_content = "\n".join(pipelock.pipelock_effective_allowlist(bottle)) + "\n"
        # Current Dockerfile for the agent image. Read from the repo
        # root; for `--cwd` derived images the base Dockerfile is what
        # the agent should propose changes against (the derived layer
        # is just a workspace copy).
        dockerfile_path = Path(__file__).resolve().parent.parent.parent.parent / "Dockerfile"
        dockerfile_content = dockerfile_path.read_text() if dockerfile_path.is_file() else ""
        supervise_plan = supervise.prepare(
            slug, stage_dir,
            routes_content=routes_content,
            allowlist_content=allowlist_content,
            dockerfile_content=dockerfile_content,
        )
    resolved = resolve_env(manifest, spec.agent_name)
    # Everything that should reach the bottle by-name (so its value
    # never lands on argv or in env_file) goes into one dict. Nothing
    # mutates the host os.environ.
    forwarded_env: dict[str, str] = dict(resolved.forwarded)
    # When the bottle declares an egress-proxy route for the Anthropic
    # OAuth flow, claude-code's outbound Authorization gets stripped +
    # re-injected by egress-proxy. The agent's environ still needs
    # *something* claude-code recognises as a credential or it refuses
    # to start; ship a non-secret placeholder. The placeholder is not
    # any real `auth.token_ref` value, so leaking it would tell an
    # attacker only that egress-proxy is in front.
    has_anthropic_auth = any(
        r.token_ref == "CLAUDE_CODE_OAUTH_TOKEN"
        for r in egress_proxy_plan.routes
    )
    if has_anthropic_auth:
        forwarded_env["CLAUDE_CODE_OAUTH_TOKEN"] = "egress-proxy-placeholder"
        # Belt-and-braces: turn off telemetry endpoints (statsig,
        # error reporting) that egress-proxy can't gate by auth.
        forwarded_env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        forwarded_env.setdefault("DISABLE_ERROR_REPORTING", "1")
    _write_env_file(resolved, env_file)
    prompt_file.write_text(agent.prompt)

    allowlist_summary = pipelock.pipelock_allowlist_summary(bottle)
    use_runsc = docker_mod.runsc_available()

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
        proxy_plan=proxy_plan,
        git_gate_plan=git_gate_plan,
        egress_proxy_plan=egress_proxy_plan,
        supervise_plan=supervise_plan,
        allowlist_summary=allowlist_summary,
        use_runsc=use_runsc,
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
