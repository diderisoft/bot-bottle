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

from ... import pipelock
from ...env import ResolvedEnv, resolve_env
from ...log import die
from .. import BottleSpec
from . import util as docker_mod
from .bottle_plan import DockerBottlePlan
from .cred_proxy import (
    DockerCredProxy,
    cred_proxy_container_name,
    cred_proxy_url,
)
from .git_gate import DockerGitGate, git_gate_container_name
from .pipelock import DockerPipelockProxy, pipelock_container_name


def resolve_plan(
    spec: BottleSpec,
    *,
    stage_dir: Path,
    proxy: DockerPipelockProxy,
    git_gate: DockerGitGate,
    cred_proxy: DockerCredProxy,
) -> DockerBottlePlan:
    """Resolve Docker-specific names and write scratch files. Trusts
    that the agent and its skills/git-gate keys are present —
    validation already ran in the base class."""
    docker_mod.require_docker()

    manifest = spec.manifest
    agent = manifest.agents[spec.agent_name]
    bottle = manifest.bottle_for(spec.agent_name)

    slug = docker_mod.slugify(spec.agent_name)

    image = os.environ.get("CLAUDE_BOTTLE_IMAGE", "claude-bottle:latest")
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
    # pipelock always; git-gate when bottle.git is non-empty; cred-proxy
    # when bottle.cred_proxy.routes is non-empty.
    sidecar_probes: list[tuple[str, str]] = [
        ("pipelock", pipelock_container_name(slug)),
    ]
    if bottle.git:
        sidecar_probes.append(("git-gate", git_gate_container_name(slug)))
    if bottle.cred_proxy.routes:
        sidecar_probes.append(("cred-proxy", cred_proxy_container_name(slug)))
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
    cred_proxy_plan = cred_proxy.prepare(bottle, slug, stage_dir)
    resolved = resolve_env(manifest, spec.agent_name)
    # Everything that should reach the bottle by-name (so its value
    # never lands on argv or in env_file) goes into one dict. Nothing
    # mutates the host os.environ.
    forwarded_env: dict[str, str] = dict(resolved.forwarded)
    # Find the (at most one) cred-proxy route claiming the
    # anthropic-base-url role. Manifest validation enforces the
    # singleton constraint. cred-proxy is the only path the Anthropic
    # OAuth token reaches the bottle — there is no fallback that
    # forwards it into the agent's environ directly. Bottles that
    # need claude-code to authenticate must declare an
    # anthropic-base-url route.
    anthropic_route = next(
        (r for r in cred_proxy_plan.routes if "anthropic-base-url" in r.roles),
        None,
    )
    if anthropic_route is not None:
        # Point claude-code at the cred-proxy. The sidecar holds the
        # OAuth token; the agent's environ does not. Strip the
        # trailing slash so claude-code's path-join produces e.g.
        # http://cred-proxy:9099/anthropic/v1/messages.
        forwarded_env["ANTHROPIC_BASE_URL"] = (
            f"{cred_proxy_url()}{anthropic_route.path}".rstrip("/")
        )
        # claude-code refuses to start without *some* credential in
        # its env. The proxy strips inbound Authorization on every
        # request and injects the real one — so a non-secret
        # placeholder is sufficient and the SC1 test still holds
        # (the placeholder is not a `cred_proxy.routes[].TokenRef`
        # value). The agent cannot exfiltrate this string because
        # it carries no meaning to api.anthropic.com.
        forwarded_env["CLAUDE_CODE_OAUTH_TOKEN"] = "cred-proxy-placeholder"
        # Belt-and-braces: turn off telemetry endpoints that don't
        # route through ANTHROPIC_BASE_URL (statsig, error reporting).
        # PRD 0010 open question default.
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
        env_file=env_file,
        forwarded_env=forwarded_env,
        prompt_file=prompt_file,
        proxy_plan=proxy_plan,
        git_gate_plan=git_gate_plan,
        cred_proxy_plan=cred_proxy_plan,
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
