"""DockerCredProxy — the Docker-specific lifecycle for the per-bottle
cred-proxy sidecar (PRD 0010). Inherits the platform-agnostic prepare
step (upstream lift + routes.json render + token-env-map derivation)
from `CredProxy`."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ...cred_proxy import (
    CredProxy,
    CredProxyPlan,
    cred_proxy_resolve_token_values,
)
from ...log import die, info, warn
from . import util as docker_mod


CRED_PROXY_IMAGE = os.environ.get(
    "CLAUDE_BOTTLE_CRED_PROXY_IMAGE",
    "claude-bottle-cred-proxy:latest",
)

CRED_PROXY_DOCKERFILE = "Dockerfile.cred-proxy"

# Listening port inside the sidecar. The agent dials cred-proxy on
# this port; surfaced as a constant so the provisioner and tests can
# both reference it.
CRED_PROXY_PORT = int(os.environ.get("CLAUDE_BOTTLE_CRED_PROXY_PORT", "9099"))

# DNS name agents use to reach the sidecar. Attached as a
# --network-alias on the internal docker network so the URL the
# provisioner writes into the agent's environ is stable across
# bottles (the container name carries the per-bottle slug; the alias
# does not).
CRED_PROXY_HOSTNAME = "cred-proxy"

# In-container path the proxy server reads its route table from.
# Pre-created in Dockerfile.cred-proxy so `docker cp` can drop the
# file directly.
CRED_PROXY_ROUTES_IN_CONTAINER = "/run/cred-proxy/routes.json"

# Repo root, for `docker build` context. Resolved from this file's
# location: claude_bottle/backend/docker/cred_proxy.py → repo root.
_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)


def cred_proxy_container_name(slug: str) -> str:
    return f"claude-bottle-cred-proxy-{slug}"


def cred_proxy_url() -> str:
    """Base URL the agent dials. Stable across bottles because the
    sidecar attaches `--network-alias cred-proxy` on the internal
    network; the container name (which carries the slug) is not
    referenced by agent-side config."""
    return f"http://{CRED_PROXY_HOSTNAME}:{CRED_PROXY_PORT}"


def build_cred_proxy_image() -> None:
    """Build the cred-proxy image from `Dockerfile.cred-proxy`.
    Called by `DockerCredProxy.start`; exposed at module level so
    integration tests can build it without running the full launch
    pipeline."""
    docker_mod.build_image(CRED_PROXY_IMAGE, _REPO_DIR, dockerfile=CRED_PROXY_DOCKERFILE)


class DockerCredProxy(CredProxy):
    """Brings the cred-proxy sidecar up and down via Docker."""

    def start(self, plan: CredProxyPlan) -> str:
        """Boot the cred-proxy sidecar:
          1. Resolve every host TokenRef env var into a concrete
             value. Fails early if any are unset.
          2. Build the cred-proxy image (no-op when cache is hot).
          3. `docker create` on the internal network with
             `--network-alias cred-proxy` and one `-e CRED_PROXY_TOKEN_N`
             flag per route. The values arrive via subprocess env, so
             they never land on argv.
          4. `docker cp` the routes.json into the container.
          5. Attach to the per-agent egress network so the proxy can
             reach the real upstream over HTTPS.
          6. `docker start`.
        Returns the container name (the target passed to `.stop`)."""
        if not plan.upstreams:
            die("DockerCredProxy.start called with no upstreams; caller should skip")
        if not plan.internal_network or not plan.egress_network:
            die(
                "DockerCredProxy.start: internal_network / egress_network must be "
                "populated on the plan before start"
            )
        if not plan.routes_path.is_file():
            die(
                f"cred-proxy routes file missing at {plan.routes_path}; "
                f"CredProxy.prepare must run first"
            )

        # Resolve host env vars into concrete values. This must
        # happen at start time (not prepare) — the values flow into
        # the sidecar's environ via subprocess env. The plan never
        # holds them.
        token_values = cred_proxy_resolve_token_values(plan.token_env_map, dict(os.environ))

        build_cred_proxy_image()

        name = cred_proxy_container_name(plan.slug)
        info(f"starting cred-proxy sidecar {name} on network {plan.internal_network}")

        create_args = [
            "docker", "create",
            "--name", name,
            "--network", plan.internal_network,
            "--network-alias", CRED_PROXY_HOSTNAME,
        ]
        # One -e flag per token slot; values arrive via subprocess env.
        # docker create with `-e NAME` (no =VALUE) reads NAME from the
        # current process env at create time. We pass `env=child_env`
        # to subprocess.run so the value comes from token_values, not
        # the host's os.environ directly — keeps the resolver in one
        # place and lets cred_proxy_resolve_token_values surface
        # missing-env errors with a clear hint.
        for token_env in sorted(plan.token_env_map.keys()):
            create_args.extend(["-e", token_env])
        create_args.append(CRED_PROXY_IMAGE)

        child_env: dict[str, str] = {**os.environ, **token_values}

        if subprocess.run(
            create_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            check=False,
        ).returncode != 0:
            die(f"failed to create cred-proxy sidecar {name}")

        cp_result = subprocess.run(
            ["docker", "cp", str(plan.routes_path),
             f"{name}:{CRED_PROXY_ROUTES_IN_CONTAINER}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if cp_result.returncode != 0:
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            die(
                f"failed to copy routes.json into {name}: "
                f"{cp_result.stderr.strip()}"
            )

        if subprocess.run(
            ["docker", "network", "connect", plan.egress_network, name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode != 0:
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            die(
                f"failed to attach cred-proxy sidecar {name} to egress network "
                f"{plan.egress_network}"
            )

        if subprocess.run(
            ["docker", "start", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode != 0:
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            die(f"failed to start cred-proxy sidecar {name}")

        return name

    def stop(self, target: str) -> None:
        """Idempotent: missing container is success. `target` is the
        container name returned by `.start`."""
        if subprocess.run(
            ["docker", "inspect", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0:
            if subprocess.run(
                ["docker", "rm", "-f", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode != 0:
                warn(
                    f"failed to remove cred-proxy sidecar {target}; "
                    f"clean up with 'docker rm -f {target}'"
                )
