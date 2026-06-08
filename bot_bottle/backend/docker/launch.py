"""Launch step for the Docker bottle backend.

PRD 0018 chunk 3: each instance is one `docker compose` project.

The flow is:

  1. Build the agent's base + derived image (compose builds the
     sidecar images via the `build:` directive on first up).
  2. Mint the per-bottle egress CA (chunk 2 writes it under
     state/<slug>/egress/).
  3. Populate the inner plans with launch-time fields so the
     renderer can read network names, CA paths.
  6. Render the compose spec, write it to
     state/<slug>/docker-compose.yml, write metadata.json.
  7. `docker compose up -d` (token + OAuth values flow into the
     compose subprocess env so `environment: [NAME]` bare-name
     entries inherit without rendering values into the file).
  8. Provision (CA install, prompt copy, skills, git, supervise
     config) — unchanged, uses `docker exec`.
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

from ...egress import egress_resolve_token_values
from ...git_gate import revoke_git_gate_provisioned_keys
from ...log import info, warn
from . import network as network_mod
from . import util as docker_mod
from .bottle import DockerBottle
from .bottle_plan import DockerBottlePlan
from ...bottle_state import (
    bottle_state_dir,
    egress_state_dir,
    git_gate_state_dir,
)
from .compose import (
    bottle_plan_to_compose,
    compose_down,
    compose_dump_logs,
    compose_file_path,
    compose_log_path,
    compose_project_name,
    compose_up,
    write_compose_file,
)
from .egress import egress_tls_init


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

    _bottle_for_revoke = plan.spec.manifest.bottle_for(plan.spec.agent_name)
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
        # Step 1: agent image build. Sidecar images get built lazily by
        # `docker compose up` via the renderer's `build:` directives.
        docker_mod.build_image(
            plan.image, _REPO_DIR,
            dockerfile=plan.dockerfile_path,
        )

        internal_network = network_mod.network_name_for_slug(plan.slug)
        egress_network = network_mod.network_egress_name_for_slug(plan.slug)

        egress_ca_host, egress_ca_cert_only = egress_tls_init(
            egress_state_dir(plan.slug),
        )

        git_gate_plan = plan.git_gate_plan
        if git_gate_plan.upstreams:
            git_gate_plan = dataclasses.replace(
                git_gate_plan,
                internal_network=internal_network,
                egress_network=egress_network,
            )
        egress_plan = dataclasses.replace(
            plan.egress_plan,
            internal_network=internal_network,
            egress_network=egress_network,
            mitmproxy_ca_host_path=egress_ca_host,
            mitmproxy_ca_cert_only_host_path=egress_ca_cert_only,
        )
        supervise_plan = plan.supervise_plan
        if supervise_plan is not None:
            supervise_plan = dataclasses.replace(
                supervise_plan,
                internal_network=internal_network,
            )
        plan = dataclasses.replace(
            plan,
            git_gate_plan=git_gate_plan,
            egress_plan=egress_plan,
            supervise_plan=supervise_plan,
        )

        # Step 6: render + write the compose file. metadata.json
        # was written at prepare time and already carries
        # compose_project; nothing to update here.
        state_dir = bottle_state_dir(plan.slug)
        spec = bottle_plan_to_compose(plan)
        compose_file = write_compose_file(spec, compose_file_path(state_dir))
        project = compose_project_name(plan.slug)

        # Step 7: compose up. Token values + the OAuth placeholder
        # flow through subprocess env; the compose file holds only
        # bare names for the secret-carrying entries.
        effective_env = {**dict(os.environ), **plan.agent_provision.provisioned_env}
        token_values = egress_resolve_token_values(
            plan.egress_plan.token_env_map, effective_env,
        )
        compose_env: dict[str, str] = {
            **os.environ,
            **plan.forwarded_env,
            **token_values,
        }
        info(
            f"docker compose up -d  (project {project}, "
            f"{len(spec['services'])} services)"
        )
        compose_up(project, compose_file, env=compose_env)

        # Register teardown in reverse order: log dump first, then
        # `compose down`. Networks come down last via callbacks
        # registered in step 2.
        stack.callback(compose_down, project, compose_file)
        stack.callback(
            compose_dump_logs, project, compose_file, compose_log_path(state_dir),
        )

        # Step 8: provision. Create the bottle first so provisioners
        # can use bottle.exec / bottle.cp_in; set the prompt path
        # returned by provision_prompt after the fact.
        bottle = DockerBottle(
            plan.container_name,
            teardown,
            None,
            agent_command=plan.agent_command,
            agent_prompt_mode=plan.agent_prompt_mode,
        )
        bottle.prompt_path = provision(plan, bottle)

        # Step 9: yield. exec_agent continues to use `docker exec -it`
        # — the agent runs `sleep infinity` per the renderer's
        # service spec.
        yield bottle
    finally:
        teardown()
