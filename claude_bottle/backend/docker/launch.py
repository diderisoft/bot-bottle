"""Launch step for the Docker bottle backend.

`launch` is a context manager: builds the image(s), creates the per-
agent networks, brings up the pipelock sidecar, starts the agent
container, then runs the provision step. Teardown is sequenced via an
ExitStack so callbacks fire in reverse-order of registration even if
something raises mid-bring-up.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Callable, Generator

from ...log import die, info
from . import network as network_mod
from . import util as docker_mod
from .bottle import DockerBottle
from .bottle_plan import DockerBottlePlan
from .pipelock import DockerPipelockProxy, pipelock_proxy_url, pipelock_tls_init


# Where the repo root lives, for `docker build` context. Computed once.
_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)


@contextmanager
def launch(
    plan: DockerBottlePlan,
    *,
    proxy: DockerPipelockProxy,
    provision: Callable[[DockerBottlePlan, str], str | None],
) -> Generator[DockerBottle, None, None]:
    """Build, launch, and provision a Docker bottle. Teardown on exit.

    `provision` is the backend's provision orchestrator (passed in so
    this module stays free of backend-class plumbing)."""
    stack = ExitStack()

    def teardown() -> None:
        try:
            stack.close()
        except BaseException:
            # Teardown must not raise; swallow so the caller's
            # __exit__ path can still propagate the original error.
            pass

    try:
        docker_mod.build_image(plan.image, _REPO_DIR)
        if plan.derived_image:
            docker_mod.build_image_with_cwd(
                plan.derived_image, plan.image, plan.spec.user_cwd
            )

        internal_network = network_mod.network_create_internal(plan.slug)
        stack.callback(network_mod.network_remove, internal_network)

        egress_network = network_mod.network_create_egress(plan.slug)
        stack.callback(network_mod.network_remove, egress_network)

        # Per-bottle ephemeral CA for pipelock's TLS interception
        # (PRD 0006). One-shot pipelock container writes ca.pem +
        # ca-key.pem under plan.stage_dir; .start docker-cp's them
        # into the sidecar. The private key never leaves the host
        # stage dir, which start.py's outer finally `shutil.rmtree`s
        # after the sidecar is torn down.
        ca_cert_host, ca_key_host = pipelock_tls_init(plan.stage_dir)
        proxy_plan = dataclasses.replace(
            plan.proxy_plan,
            internal_network=internal_network,
            egress_network=egress_network,
            ca_cert_host_path=ca_cert_host,
            ca_key_host_path=ca_key_host,
        )
        pipelock_name = proxy.start(proxy_plan)
        stack.callback(proxy.stop, pipelock_name)

        container = _run_agent_container(plan, internal_network)
        stack.callback(docker_mod.force_remove_container, container)

        prompt_path = provision(plan, container)

        yield DockerBottle(container, teardown, prompt_path)
    finally:
        teardown()


def _run_agent_container(plan: DockerBottlePlan, internal_network: str) -> str:
    """Build the `docker run` argv and execute it, handling name-
    conflict races by incrementing the suffix (unless the name was
    user-pinned). Returns the resolved container name."""
    proxy_url = pipelock_proxy_url(plan.slug)
    docker_args: list[str] = [
        "--rm", "-d",
        "--name", plan.container_name,
        "--network", internal_network,
        "-e", f"HTTPS_PROXY={proxy_url}",
        "-e", f"HTTP_PROXY={proxy_url}",
        "-e", "NO_PROXY=localhost,127.0.0.1",
    ]
    if plan.use_runsc:
        docker_args.extend(["--runtime", "runsc"])
    if plan.env_file.stat().st_size > 0:
        docker_args.extend(["--env-file", str(plan.env_file)])
    for name in plan.forwarded_env:
        docker_args.extend(["-e", name])

    docker_args.extend([plan.runtime_image, "sleep", "infinity"])

    info(f"starting container {plan.container_name} from {plan.runtime_image}")

    # Inject forwarded values (secrets, interpolated host vars, the
    # renamed OAuth token) into the docker-run child's env so the
    # `-e NAME` flags above pick them up — without touching our own
    # os.environ or putting values on argv.
    child_env: dict[str, str] = {**os.environ, **plan.forwarded_env}

    name_idx = docker_args.index("--name") + 1
    for candidate in docker_mod.container_name_candidates(plan.container_name):
        docker_args[name_idx] = candidate
        run_result = subprocess.run(
            ["docker", "run", *docker_args],
            capture_output=True,
            text=True,
            env=child_env,
            check=False,
        )
        if run_result.returncode == 0:
            return candidate
        err_text = run_result.stderr
        if plan.container_name_pinned or "is already in use" not in err_text:
            sys.stderr.write(err_text + "\n")
            die(f"docker run failed for container '{candidate}'")
        info(f"name conflict on {candidate}; retrying with next candidate")
    die(
        f"could not find a free container name after "
        f"{plan.container_name}-{docker_mod.MAX_CONTAINER_SUFFIX} retries; "
        f"clean up old containers"
    )


