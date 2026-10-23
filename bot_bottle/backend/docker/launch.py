"""Launch step for the Docker bottle backend.

Each bottle is one `docker compose` project holding only the agent, on the
shared gateway network. Per bottle:

  1. resolve the agent image (committed snapshot, published image, cached
     image, or a fresh build) — `build_or_load_images`;
  2. mint the bottle's git-gate deploy keys;
  3. register the bottle with the orchestrator, which pins its source IP on
     the shared gateway network and provisions its git-gate state;
  4. stage the shared gateway CA and point the agent at the gateway;
  5. render the agent-only compose file and `docker compose up -d`
     (forwarded values flow through the compose subprocess env as bare
     names, so they are never written to the file);
  6. provision (CA install, prompt, skills, workspace, git, supervise)
     with `docker exec` / `docker cp`, then yield a `DockerBottle`.

Teardown (ExitStack callbacks fire in reverse): dump the compose logs to
state/<slug>/compose.log, `docker compose down`, deregister the bottle from
the orchestrator, then revoke its deploy keys.
"""

from __future__ import annotations

import dataclasses
import functools
import os
from contextlib import contextmanager
from typing import Callable, Generator

from ...agent_provider import runtime_for
from ...image_cache import check_stale
from ...lib.log import die, info
from .. import BottleImages, isolation_probe, oci_agent_image
from ..launch_common import (
    LaunchTeardown,
    registration_tokens,
    gateway_agent_urls,
    provision_git_gate_keys,
    stage_gateway_ca,
    terminal_title,
)
from . import util as docker_mod
from .bottle import DockerBottle
from .bottle_plan import DockerBottlePlan
from ...bottle_state import bottle_state_dir, read_committed_image
from .compose import (
    compose_down,
    compose_dump_logs,
    compose_file_path,
    compose_log_path,
    compose_project_name,
    compose_up,
    write_compose_file,
)
from .consolidated_compose import consolidated_agent_compose
from ...orchestrator.store.config_store import resolve_teardown_timeout
from ...paths import host_db_path
from .consolidated_launch import launch_consolidated, deprovision_consolidated
from .infra import INFRA_NAME, DockerInfraService
from .gateway import DockerGateway
from ...contract.hostctl import Settings
from ... import resources


def _docker_runtime(settings: Settings) -> oci_agent_image.OciRuntime:
    return oci_agent_image.OciRuntime(
        pull_argv=("docker", "pull"),
        image_exists=docker_mod.image_exists,
        tag_image=docker_mod.tag_image,
        build_image=functools.partial(
            docker_mod.build_image, no_cache=settings.operation.no_cache),
        verify=docker_mod.verify_agent_image,
    )


def build_or_load_images(plan: DockerBottlePlan) -> BottleImages:
    """Resolve the agent image ref for this plan.

    Returns the committed snapshot if one exists, the cached image when the
    policy is 'cached', or builds a fresh image and returns that."""
    committed = read_committed_image(plan.slug)
    if committed and docker_mod.image_exists(committed):
        info(f"using committed image {committed!r}")
        return BottleImages(agent=committed)
    smoke = runtime_for(plan.agent_provider_template).smoke_test
    # Prefer the release-pinned published image (or an overlay on it) over a
    # local build. None: build the bottle's own Dockerfile.
    published = oci_agent_image.resolve_published_agent_image(
        plan, _docker_runtime(plan.settings), template=plan.agent_provider_template,
        image=plan.image,
        smoke_test=smoke,
    )
    if published is not None:
        return BottleImages(agent=published)
    if plan.spec.image_policy == "prefer-cached" and docker_mod.image_exists(plan.image):
        info(f"using cached agent image {plan.image!r}")
        return BottleImages(agent=plan.image)
    if plan.spec.image_policy == "cached":
        if not docker_mod.image_exists(plan.image):
            die(
                f"cached agent image {plan.image!r} not found; "
                "run without --cached-images to build it"
            )
        info(f"using cached agent image {plan.image!r}")
        return BottleImages(agent=plan.image)
    docker_mod.build_image(plan.image, str(resources.build_root()), dockerfile=plan.dockerfile_path,
                           no_cache=plan.settings.operation.no_cache)
    docker_mod.verify_agent_image(
        plan.image, runtime_for(plan.agent_provider_template).smoke_test,
    )
    return BottleImages(agent=plan.image)


@contextmanager
def launch(
    plan: DockerBottlePlan,
    images: BottleImages,
    *,
    provision: Callable[[DockerBottlePlan, "DockerBottle"], str | None],
) -> Generator[DockerBottle, None, None]:
    """Launch and provision a Docker bottle via compose. Teardown on exit."""
    # Stamp the resolved agent image ref into the plan so compose rendering
    # picks up the right image (may be a committed snapshot or cached ref).
    plan = dataclasses.replace(
        plan,
        agent_provision=dataclasses.replace(plan.agent_provision, image=str(images.agent)),
    )

    # Docker teardown reports a failure but never raises it.
    teardown = LaunchTeardown(
        plan, f"container {plan.container_name} (compose-down)", reraise=False)
    stack = teardown.stack

    try:
        # Step 2: mint the git-gate dynamic (gitea) deploy keys, if any, before
        # provisioning the bottle's repos into the shared gateway.
        git_gate_plan = provision_git_gate_keys(plan)

        # Step 3: register on the orchestrator + provision this bottle's
        # git-gate state into the shared gateway; get the agent's attach
        # context (pinned source IP, gateway address, shared network).
        token_values = registration_tokens(plan)
        teardown_timeout = resolve_teardown_timeout(host_db_path())
        service = DockerInfraService(plan.settings)
        ctx = launch_consolidated(
            plan.egress_plan, git_gate_plan, service=service,
            tokens=token_values,
        )
        stack.callback(deprovision_consolidated, ctx.bottle_id, service=service,
                       timeout=teardown_timeout)

        # Step 4: stage the shared gateway CA for the agent — read out of the
        # running gateway container (INFRA_NAME aliases the gateway container;
        # the orchestrator holds no CA) — and point the agent's git-gate and
        # supervise URLs at the gateway.
        egress_plan = stage_gateway_ca(
            plan, DockerGateway(settings=plan.settings, name=INFRA_NAME).ca_cert_pem())
        git_gate_url, supervise_url = gateway_agent_urls(
            plan, ctx.gateway_ip, git_gate_plan)
        plan = dataclasses.replace(
            plan,
            git_gate_plan=git_gate_plan,
            egress_plan=egress_plan,
            agent_git_gate_url=git_gate_url,
            agent_supervise_url=supervise_url,
            identity_token=ctx.identity_token,
            env_var_secret=ctx.env_var_secret,
        )

        # Step 5: render + up the agent-only compose, pinned on the shared
        # gateway network and proxied through the gateway's address.
        state_dir = bottle_state_dir(plan.slug)
        spec = consolidated_agent_compose(
            plan, gateway_ip=ctx.gateway_ip, source_ip=ctx.source_ip, network=ctx.network,
        )
        compose_file = write_compose_file(spec, compose_file_path(state_dir))
        project = compose_project_name(plan.slug)
        # Forwarded vars (OAuth token, host interpolations) flow through the
        # subprocess env as bare names so values never land in the file.
        # ENV_VAR_SECRET follows the same pattern: bare name in the compose
        # spec, value only in the subprocess env so it is never written to disk.
        compose_env: dict[str, str] = {**os.environ, **plan.forwarded_env}
        if plan.env_var_secret:
            from ...orchestrator.store.secret_store import ENV_VAR_SECRET_NAME
            compose_env[ENV_VAR_SECRET_NAME] = plan.env_var_secret
        info(
            f"docker compose up -d  (project {project}, agent on shared "
            f"gateway {ctx.gateway_ip}, ip {ctx.source_ip})"
        )
        compose_up(project, compose_file, env=compose_env)
        stack.callback(compose_down, project, compose_file)
        stack.callback(
            compose_dump_logs, project, compose_file, compose_log_path(state_dir),
        )

        # Step 6: prove the agent has no route off the box except through the
        # gateway, then provision (CA install uses the gateway CA) + yield.
        bottle = DockerBottle(
            plan.container_name,
            teardown,
            None,
            agent_command=plan.agent_command,
            agent_prompt_mode=plan.agent_prompt_mode,
            agent_provider_template=plan.agent_provider_template,
            terminal_title=terminal_title(plan.spec),
            terminal_color=plan.spec.color,
            agent_workdir=plan.workspace_plan.workdir,
        )
        isolation_probe.verify_isolation(
            lambda script: bottle.exec(script).stdout,
            what=f"agent container {plan.container_name}",
            remedy=(f"Its network ({ctx.network}) must be a docker `--internal` "
                    "network; stop running bottles and relaunch so it is recreated."),
            check_dns=True,
        )
        bottle.prompt_path = provision(plan, bottle)
        bottle.registry_id = ctx.bottle_id
        yield bottle
    finally:
        teardown()


def stale_checks(plan: DockerBottlePlan) -> None:
    """Raise StaleImageError if a cached image is older than the configured
    threshold. Only runs when image_policy is 'cached'. Called by the backend
    class's _image_stale_checks before _launch_impl starts any resources."""
    if plan.spec.image_policy not in ("cached", "prefer-cached"):
        return
    committed = read_committed_image(plan.slug)
    if committed and docker_mod.image_exists(committed):
        ts = docker_mod.image_created_at(committed)
        if ts is not None:
            check_stale(f"agent image {committed!r}", ts)
        return
    if docker_mod.image_exists(plan.image):
        ts = docker_mod.image_created_at(plan.image)
        if ts is not None:
            check_stale(f"agent image {plan.image!r}", ts)
