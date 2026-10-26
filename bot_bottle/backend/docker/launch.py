"""Docker launch: each bottle is a compose project holding only the agent, on
the shared gateway network.

Resolve the image, mint deploy keys, register (pinning the source IP and
provisioning git-gate), stage the CA, `docker compose up -d`, run the isolation
probe, provision, and yield a `DockerBottle`. Teardown, in reverse: dump logs
to `state/<slug>/compose.log`, `compose down`, deregister, revoke keys.
"""

from __future__ import annotations

import dataclasses
import functools
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Callable, Generator

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

if TYPE_CHECKING:
    from ..adoption import AdoptionRecord


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
    """The agent image ref: the committed snapshot, the cached image under the
    'cached' policy, or a fresh build."""
    committed = read_committed_image(plan.slug)
    if committed and docker_mod.image_exists(committed):
        info(f"using committed image {committed!r}")
        return BottleImages(agent=committed)
    smoke = runtime_for(plan.harness).smoke_test
    # Prefer the published image (or an overlay on it); None means build the
    # bottle's Dockerfile.
    published = oci_agent_image.resolve_published_agent_image(
        plan, _docker_runtime(plan.settings), harness=plan.harness,
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
        plan.image, runtime_for(plan.harness).smoke_test,
    )
    return BottleImages(agent=plan.image)


@contextmanager
def launch(
    plan: DockerBottlePlan,
    images: BottleImages,
    *,
    provision: Callable[[DockerBottlePlan, "DockerBottle"], str | None],
) -> Generator[DockerBottle, None, None]:
    """Launch and provision a docker bottle. Tears down on exit."""
    # Stamp the resolved image into the plan for compose.
    plan = dataclasses.replace(
        plan,
        agent_provision=dataclasses.replace(plan.agent_provision, image=str(images.agent)),
    )

    # Docker teardown reports failures but never raises.
    teardown = LaunchTeardown(
        plan, f"container {plan.container_name} (compose-down)", reraise=False)
    stack = teardown.stack

    try:
        # Mint deploy keys before provisioning repos into the gateway.
        git_gate_plan = provision_git_gate_keys(plan)

        # Register, provision git-gate state, and get the attach context.
        token_values = registration_tokens(plan)
        teardown_timeout = resolve_teardown_timeout(host_db_path())
        service = DockerInfraService(plan.settings)
        ctx = launch_consolidated(
            plan.egress_plan, git_gate_plan, service=service,
            tokens=token_values,
        )
        stack.callback(deprovision_consolidated, ctx.bottle_id, service=service,
                       timeout=teardown_timeout)

        # Stage the gateway's CA (the orchestrator holds none) and point the
        # agent's git-gate and supervise URLs at the gateway.
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

        # Write and start the agent-only compose project.
        state_dir = bottle_state_dir(plan.slug)
        spec = consolidated_agent_compose(
            plan, gateway_ip=ctx.gateway_ip, source_ip=ctx.source_ip, network=ctx.network,
        )
        compose_file = write_compose_file(spec, compose_file_path(state_dir))
        project = compose_project_name(plan.slug)
        # Forwarded values and ENV_VAR_SECRET go through the subprocess env,
        # never the file.
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

        # Prove the only way out is the gateway, then provision and yield.
        bottle = DockerBottle(
            plan.container_name,
            teardown,
            None,
            agent_command=plan.agent_command,
            agent_prompt_mode=plan.agent_prompt_mode,
            harness=plan.harness,
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
        bottle.env_var_secret = getattr(ctx, "env_var_secret", "")
        yield bottle
    finally:
        teardown()


@contextmanager
def adopted(
    bottle_id: str, record: "AdoptionRecord", settings: Settings,
) -> Generator[DockerBottle, None, None]:
    """A bottle a previous host controller launched: exec into it, then tear it
    down as its holder would have (compose down, then deregister)."""
    try:
        yield attach(bottle_id, record)
    finally:
        compose_down(compose_project_name(bottle_id),
                     compose_file_path(bottle_state_dir(bottle_id)))
        if record.registry_id:
            deprovision_consolidated(record.registry_id, service=DockerInfraService(settings),
                                     timeout=resolve_teardown_timeout(host_db_path()))


def attach(bottle_id: str, record: "AdoptionRecord") -> DockerBottle:
    """A bottle a previous host controller launched, to exec into."""
    del bottle_id
    return DockerBottle(record.container, lambda: None, None)


def stale_checks(plan: DockerBottlePlan) -> None:
    """Raise StaleImageError if a cached image is past the threshold. Only
    under the 'cached' policy."""
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
