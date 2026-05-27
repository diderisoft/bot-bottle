"""Launch step for the Docker bottle backend.

PRD 0018 chunk 3: each instance is one `docker compose` project.

The flow is:

  1. Build the agent's base + derived image (compose builds the
     sidecar images via the `build:` directive on first up).
  2. Pre-create the per-bottle networks. We do this outside compose
     so we can inspect the assigned internal CIDR and embed it in
     pipelock's yaml (compose's `external: true` lets the compose
     file reference these pre-existing networks).
  3. Mint the per-bottle CAs (chunk 2 writes them under
     state/<slug>/{pipelock,egress}/).
  4. Re-render pipelock yaml with the now-known internal CIDR so
     the SSRF allowlist exempts the bottle's own subnet.
  5. Populate the inner plans with launch-time fields so the
     renderer can read network names, CA paths, pipelock URL.
  6. Render the compose spec, write it to
     state/<slug>/docker-compose.yml, write metadata.json.
  7. `docker compose up -d` (token + OAuth values flow into the
     compose subprocess env so `environment: [NAME]` bare-name
     entries inherit without rendering values into the file).
  8. Provision (CA install, prompt copy, skills, git, supervise
     config) — unchanged, uses `docker exec`.
  9. Yield a DockerBottle handle. `exec_claude` runs claude via
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
from ...log import info
from . import network as network_mod
from . import util as docker_mod
from .bottle import DockerBottle
from .bottle_plan import DockerBottlePlan
from .bottle_state import (
    bottle_state_dir,
    egress_state_dir,
    pipelock_state_dir,
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
from .pipelock import (
    pipelock_proxy_url,
    pipelock_tls_init,
)


# Where the repo root lives, for `docker build` context. Computed once.
_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)


@contextmanager
def launch(
    plan: DockerBottlePlan,
    *,
    provision: Callable[[DockerBottlePlan, str], str | None],
) -> Generator[DockerBottle, None, None]:
    """Build, launch, and provision a Docker bottle via compose.
    Teardown on exit."""
    stack = ExitStack()

    def teardown() -> None:
        try:
            stack.close()
        except BaseException:
            # Teardown must not raise; swallow so the caller's
            # __exit__ path can still propagate the original error.
            pass

    try:
        # Step 1: agent image build. Sidecar images get built lazily by
        # `docker compose up` via the renderer's `build:` directives.
        docker_mod.build_image(
            plan.image, _REPO_DIR,
            dockerfile=plan.dockerfile_path,
        )
        if plan.derived_image:
            docker_mod.build_image_with_cwd(
                plan.derived_image, plan.image, plan.spec.user_cwd
            )

        # Networks: compose-managed. The names are derived
        # deterministically from the slug so the renderer can put
        # them on the services and `compose up` creates them with
        # those names. The empirical spike confirmed pipelock's
        # SSRF guard only checks proxied-request destinations, not
        # source IPs — so the bottle's own internal CIDR doesn't
        # need to be in `ssrf.ip_allowlist`. Pre-create + CIDR
        # introspection are gone; compose owns the network
        # lifecycle.
        internal_network = network_mod.network_name_for_slug(plan.slug)
        egress_network = network_mod.network_egress_name_for_slug(plan.slug)

        # Mint per-bottle CAs into state/<slug>/{pipelock,egress}/.
        ca_cert_host, ca_key_host = pipelock_tls_init(pipelock_state_dir(plan.slug))
        egress_ca_host, egress_ca_cert_only = egress_tls_init(
            egress_state_dir(plan.slug),
        )

        # Populate launch-time fields on every inner plan so the
        # renderer reads concrete network names, CA paths, and
        # pipelock URL.
        proxy_plan = dataclasses.replace(
            plan.proxy_plan,
            internal_network=internal_network,
            internal_network_cidr="",
            egress_network=egress_network,
            ca_cert_host_path=ca_cert_host,
            ca_key_host_path=ca_key_host,
        )
        git_gate_plan = plan.git_gate_plan
        if git_gate_plan.upstreams:
            git_gate_plan = dataclasses.replace(
                git_gate_plan,
                internal_network=internal_network,
                egress_network=egress_network,
            )
        egress_plan = plan.egress_plan
        if egress_plan.routes:
            egress_plan = dataclasses.replace(
                egress_plan,
                internal_network=internal_network,
                egress_network=egress_network,
                mitmproxy_ca_host_path=egress_ca_host,
                mitmproxy_ca_cert_only_host_path=egress_ca_cert_only,
                pipelock_ca_host_path=ca_cert_host,
                pipelock_proxy_url=pipelock_proxy_url(plan.slug),
            )
        supervise_plan = plan.supervise_plan
        if supervise_plan is not None:
            supervise_plan = dataclasses.replace(
                supervise_plan,
                internal_network=internal_network,
            )
        plan = dataclasses.replace(
            plan,
            proxy_plan=proxy_plan,
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
        token_values: dict[str, str] = {}
        if plan.egress_plan.routes:
            token_values = egress_resolve_token_values(
                plan.egress_plan.token_env_map, dict(os.environ),
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

        # Step 8: provision. Unchanged — uses `docker exec` against
        # the agent container by its known name.
        prompt_path = provision(plan, plan.container_name)

        # Step 9: yield. exec_claude continues to use `docker exec -it`
        # — the agent runs `sleep infinity` per the renderer's
        # service spec.
        yield DockerBottle(plan.container_name, teardown, prompt_path)
    finally:
        teardown()
