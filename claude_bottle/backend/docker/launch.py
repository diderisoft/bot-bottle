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
from ...pipelock import pipelock_build_config, pipelock_render_yaml
from ...supervise import CURRENT_CONFIG_DIR_IN_AGENT
from . import network as network_mod
from . import util as docker_mod
from .bottle import DockerBottle
from .bottle_plan import DockerBottlePlan
from .cred_proxy import DockerCredProxy
from .git_gate import DockerGitGate
from .pipelock import (
    PIPELOCK_CA_CERT_IN_CONTAINER,
    PIPELOCK_CA_KEY_IN_CONTAINER,
    DockerPipelockProxy,
    pipelock_proxy_url,
    pipelock_tls_init,
)
from .provision.ca import AGENT_CA_BUNDLE, AGENT_CA_PATH
from .supervise import DockerSupervise


# Where the repo root lives, for `docker build` context. Computed once.
_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)


@contextmanager
def launch(
    plan: DockerBottlePlan,
    *,
    proxy: DockerPipelockProxy,
    git_gate: DockerGitGate,
    cred_proxy: DockerCredProxy,
    supervise: DockerSupervise,
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

        # Docker assigns a CIDR to the new internal network. Pipelock's
        # SSRF guard otherwise rejects any destination resolving into
        # RFC1918 space — which includes the cred-proxy / git-gate /
        # pipelock sidecars themselves. Allowlist the bottle's own
        # internal subnet so the agent can reach its sidecars via
        # pipelock; api_allowlist + body-scanning still apply.
        internal_cidr = network_mod.network_inspect_cidr(internal_network)

        # Per-bottle ephemeral CA for pipelock's TLS interception
        # (PRD 0006). One-shot pipelock container writes ca.pem +
        # ca-key.pem under plan.stage_dir; .start docker-cp's them
        # into the sidecar. The private key never leaves the host
        # stage dir, which start.py's outer finally `shutil.rmtree`s
        # after the sidecar is torn down.
        ca_cert_host, ca_key_host = pipelock_tls_init(plan.stage_dir)

        # Re-render the pipelock yaml with the SSRF allowlist now that
        # we know the internal CIDR. Prepare wrote the yaml without
        # the ssrf block (CIDR wasn't known yet); overwrite the same
        # path so .start docker-cp's the updated content.
        bottle = plan.spec.manifest.bottle_for(plan.spec.agent_name)
        cfg = pipelock_build_config(
            bottle,
            ca_cert_path=PIPELOCK_CA_CERT_IN_CONTAINER,
            ca_key_path=PIPELOCK_CA_KEY_IN_CONTAINER,
            ssrf_ip_allowlist=(internal_cidr,),
        )
        plan.proxy_plan.yaml_path.write_text(pipelock_render_yaml(cfg))
        plan.proxy_plan.yaml_path.chmod(0o600)

        proxy_plan = dataclasses.replace(
            plan.proxy_plan,
            internal_network=internal_network,
            internal_network_cidr=internal_cidr,
            egress_network=egress_network,
            ca_cert_host_path=ca_cert_host,
            ca_key_host_path=ca_key_host,
        )
        # Re-bind the outer plan so provision_ca (which runs later
        # from `provision(plan, container)`) can read the populated
        # CA paths off plan.proxy_plan.
        plan = dataclasses.replace(plan, proxy_plan=proxy_plan)
        pipelock_name = proxy.start(plan.proxy_plan)
        stack.callback(proxy.stop, pipelock_name)

        # Git gate (PRD 0008). One sidecar per agent, only brought up
        # when the bottle has git entries. Same internal + egress
        # network attachment as the other sidecars; agent dials it as
        # `git://<container-name>/<name>.git` via the pushInsteadOf
        # rules provision_git writes into ~/.gitconfig.
        if plan.git_gate_plan.upstreams:
            git_gate_plan = dataclasses.replace(
                plan.git_gate_plan,
                internal_network=internal_network,
                egress_network=egress_network,
            )
            plan = dataclasses.replace(plan, git_gate_plan=git_gate_plan)
            git_gate_name = git_gate.start(plan.git_gate_plan)
            stack.callback(git_gate.stop, git_gate_name)

        # Cred-proxy (PRD 0010). One sidecar per bottle when
        # bottle.cred_proxy.routes is non-empty. Must come up AFTER pipelock
        # — cred-proxy routes its outbound HTTPS through pipelock
        # (HTTPS_PROXY in environ + the per-bottle CA in its trust
        # store) so the egress allowlist + body scanner sit in the
        # cred-proxy path too. Must come up BEFORE the agent so DNS
        # resolution for `cred-proxy` succeeds on the agent's first
        # call; tokens flow from the host env into the sidecar's
        # environ, not the agent's.
        if plan.cred_proxy_plan.routes:
            cred_proxy_plan = dataclasses.replace(
                plan.cred_proxy_plan,
                internal_network=internal_network,
                egress_network=egress_network,
                pipelock_ca_host_path=ca_cert_host,
                pipelock_proxy_url=pipelock_proxy_url(plan.slug),
            )
            plan = dataclasses.replace(plan, cred_proxy_plan=cred_proxy_plan)
            cred_proxy_name = cred_proxy.start(plan.cred_proxy_plan)
            stack.callback(cred_proxy.stop, cred_proxy_name)

        # Supervise sidecar (PRD 0013). Opt-in via bottle.supervise.
        # Internal-network only — the sidecar makes no outbound calls.
        # Must come up BEFORE the agent so DNS resolution for
        # `supervise` succeeds on the agent's first tool call.
        if plan.supervise_plan is not None:
            supervise_plan = dataclasses.replace(
                plan.supervise_plan,
                internal_network=internal_network,
            )
            plan = dataclasses.replace(plan, supervise_plan=supervise_plan)
            supervise_name = supervise.start(plan.supervise_plan)
            stack.callback(supervise.stop, supervise_name)

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
        # CA trust trio for the agent process. Docker propagates
        # run-time env into `docker exec`, so `claude` sees these
        # without per-exec threading. NODE_EXTRA_CA_CERTS points at
        # the cert file (Node appends it to its bundled roots);
        # SSL_CERT_FILE / REQUESTS_CA_BUNDLE point at the system
        # bundle that `update-ca-certificates` rebuilds in
        # provision_ca.
        "-e", f"NODE_EXTRA_CA_CERTS={AGENT_CA_PATH}",
        "-e", f"SSL_CERT_FILE={AGENT_CA_BUNDLE}",
        "-e", f"REQUESTS_CA_BUNDLE={AGENT_CA_BUNDLE}",
    ]
    if plan.use_runsc:
        docker_args.extend(["--runtime", "runsc"])
    if plan.env_file.stat().st_size > 0:
        docker_args.extend(["--env-file", str(plan.env_file)])
    for name in plan.forwarded_env:
        docker_args.extend(["-e", name])

    # PRD 0013: read-only current-config mount so the agent can read
    # routes.json / allowlist / Dockerfile before composing a
    # supervise tool-call proposal. Mounted from the per-bottle
    # stage_dir/current-config/ populated at prepare time.
    if plan.supervise_plan is not None:
        docker_args.extend([
            "-v",
            f"{plan.supervise_plan.current_config_dir}:{CURRENT_CONFIG_DIR_IN_AGENT}:ro",
        ])

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


