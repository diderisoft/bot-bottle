"""Launch step for the Docker bottle backend.

PRD 0018 chunk 3: each instance is one `docker compose` project.

The flow is:

  1. Build the agent image from the provider Dockerfile (compose
     builds the gateway image on first up).
  2. Mint the per-bottle egress CA (chunk 2 writes it under
     state/<slug>/egress/).
  3. Populate the inner plans with launch-time fields so the
     renderer can read network names, CA paths.
  6. Render the compose spec, write it to
     state/<slug>/docker-compose.yml, write metadata.json.
  7. `docker compose up -d` (token + OAuth values flow into the
     compose subprocess env so `environment: [NAME]` bare-name
     entries inherit without rendering values into the file).
  8. Provision (CA install, prompt copy, skills, workspace, git,
     supervise config) — unchanged, uses `docker exec` / `docker cp`.
  9. Yield a DockerBottle handle. `exec_agent` runs claude via
     `docker exec -it` exactly like the pre-compose world.

Teardown (ExitStack callbacks fire in reverse):
  - Dump `docker compose logs --no-color --timestamps` to
    state/<slug>/compose.log (best-effort).
  - `docker compose down` removes the project's containers (not the
    external networks).
  - `network_remove` deletes the two networks we pre-created.
"""

from __future__ import annotations

import dataclasses
import os
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Callable, Generator

from ...agent_provider import runtime_for
from ...egress import egress_resolve_token_values
from ...git_gate import (
    provision_git_gate_dynamic_keys,
    revoke_git_gate_provisioned_keys,
)
from ...log import info, warn
from . import util as docker_mod
from .bottle import DockerBottle
from .bottle_plan import DockerBottlePlan
from ...bottle_state import (
    bottle_state_dir,
    egress_state_dir,
    git_gate_state_dir,
    read_committed_image,
)
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
from .consolidated_launch import launch_consolidated, teardown_consolidated
from ...orchestrator.gateway import DockerGateway


# Where the repo root lives, for `docker build` context. Computed once.
_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)


@contextmanager
def launch(
    plan: DockerBottlePlan,
    *,
    provision: Callable[[DockerBottlePlan, "DockerBottle"], str | None],
) -> Generator[DockerBottle, None, None]:
    """Build, launch, and provision a Docker bottle via compose.
    Teardown on exit."""
    stack = ExitStack()

    _bottle_for_revoke = plan.manifest.bottle
    _git_gate_dir_for_revoke = git_gate_state_dir(plan.slug)

    def teardown() -> None:
        try:
            stack.close()
        except BaseException as exc:  # noqa: W0718 — teardown must not fail
            warn(
                f"teardown failed for container {plan.container_name}"
                f" (compose-down): {exc!r}"
            )
        revoke_git_gate_provisioned_keys(
            _bottle_for_revoke, _git_gate_dir_for_revoke
        )

    try:
        # Step 1: agent image. Use a committed snapshot when one exists
        # and is present in the local daemon; otherwise build from the
        # Dockerfile. (The gateway image is built by the orchestrator.)
        committed = read_committed_image(plan.slug)
        if committed and docker_mod.image_exists(committed):
            info(f"using committed image {committed!r}")
            plan = dataclasses.replace(
                plan,
                agent_provision=dataclasses.replace(plan.agent_provision, image=committed),
            )
        else:
            docker_mod.build_image(
                plan.image, _REPO_DIR,
                dockerfile=plan.dockerfile_path,
            )
            docker_mod.verify_agent_image(
                plan.image, runtime_for(plan.agent_provider_template).smoke_test,
            )

        # Step 2: mint the git-gate dynamic (gitea) deploy keys, if any, before
        # provisioning the bottle's repos into the shared gateway.
        git_gate_plan = plan.git_gate_plan
        if git_gate_plan.upstreams:
            git_gate_plan = provision_git_gate_dynamic_keys(
                plan.manifest.bottle, git_gate_plan, git_gate_state_dir(plan.slug),
            )

        # Step 3: register on the orchestrator + provision this bottle's
        # git-gate state into the shared gateway; get the agent's attach
        # context (pinned source IP, gateway address, shared network). The
        # per-bottle egress auth tokens are resolved from the host env now and
        # handed to the orchestrator (in memory) for the gateway to inject —
        # the agent never sees them.
        effective_env = {**os.environ, **plan.agent_provision.provisioned_env}
        token_values = egress_resolve_token_values(
            plan.egress_plan.token_env_map, effective_env,
        )
        ctx = launch_consolidated(
            plan.egress_plan, git_gate_plan, image_ref=plan.image, tokens=token_values,
        )
        stack.callback(
            teardown_consolidated, ctx.bottle_id, orchestrator_url=ctx.orchestrator_url,
        )

        # Step 4: install the SHARED gateway CA into the agent (replaces the
        # per-bottle CA) — read it out of the running gateway.
        ca_dir = egress_state_dir(plan.slug) / "gateway-ca"
        ca_dir.mkdir(parents=True, exist_ok=True)
        ca_file = ca_dir / "gateway-ca.pem"
        ca_file.write_text(DockerGateway(network=ctx.network).ca_cert_pem())
        egress_plan = dataclasses.replace(
            plan.egress_plan,
            mitmproxy_ca_host_path=ca_file,
            mitmproxy_ca_cert_only_host_path=ca_file,
        )
        # Point the agent's git-gate insteadOf rewrites at the shared gateway's
        # HTTP git endpoint (9420) instead of the dead per-bottle `git-gate`
        # alias. git-http + supervise on the gateway bypass the egress proxy
        # (NO_PROXY includes the gateway address).
        git_gate_url = (
            f"http://{ctx.gateway_ip}:9420" if git_gate_plan.upstreams else ""
        )
        supervise_url = (
            f"http://{ctx.gateway_ip}:9100/" if plan.supervise_plan is not None else ""
        )
        plan = dataclasses.replace(
            plan,
            git_gate_plan=git_gate_plan,
            egress_plan=egress_plan,
            agent_git_gate_url=git_gate_url,
            agent_supervise_url=supervise_url,
            identity_token=ctx.identity_token,
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
        compose_env: dict[str, str] = {**os.environ, **plan.forwarded_env}
        info(
            f"docker compose up -d  (project {project}, agent on shared "
            f"gateway {ctx.gateway_ip}, ip {ctx.source_ip})"
        )
        compose_up(project, compose_file, env=compose_env)
        stack.callback(compose_down, project, compose_file)
        stack.callback(
            compose_dump_logs, project, compose_file, compose_log_path(state_dir),
        )

        # Step 6: provision (CA install now uses the gateway CA) + yield.
        bottle = DockerBottle(
            plan.container_name,
            teardown,
            None,
            agent_command=plan.agent_command,
            agent_prompt_mode=plan.agent_prompt_mode,
            agent_provider_template=plan.agent_provider_template,
            terminal_title=f"{plan.spec.label} ({plan.spec.agent_name})" if plan.spec.label else plan.spec.agent_name,
            terminal_color=plan.spec.color,
            agent_workdir=plan.workspace_plan.workdir,
        )
        bottle.prompt_path = provision(plan, bottle)
        yield bottle
    finally:
        teardown()
