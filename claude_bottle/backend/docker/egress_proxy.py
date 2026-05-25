"""DockerEgressProxy — the Docker-specific lifecycle for the
per-bottle egress-proxy sidecar (PRD 0017). Inherits the platform-
agnostic prepare step (route lift + routes.yaml render + token-env
map derivation) from `EgressProxy`.

Chunk 1 of the PRD: the lifecycle is implemented but not yet called
from `launch.py`. Tests build the image and exercise start/stop
directly. Chunk 2 wires this in alongside the cred-proxy removal."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ...egress_proxy import (
    EGRESS_PROXY_HOSTNAME,
    EGRESS_PROXY_ROUTES_IN_CONTAINER,
    EgressProxy,
    EgressProxyPlan,
    egress_proxy_resolve_token_values,
)
from ...log import die, info, warn
from . import util as docker_mod


EGRESS_PROXY_IMAGE = os.environ.get(
    "CLAUDE_BOTTLE_EGRESS_PROXY_IMAGE",
    "claude-bottle-egress-proxy:latest",
)

EGRESS_PROXY_DOCKERFILE = "Dockerfile.egress-proxy"

# Listening port inside the sidecar. The agent's HTTP_PROXY env var
# (chunk 2) will resolve to `http://egress-proxy:<port>`.
EGRESS_PROXY_PORT = int(os.environ.get("CLAUDE_BOTTLE_EGRESS_PROXY_PORT", "9099"))

# Repo root, for `docker build` context. Resolved from this file's
# location: claude_bottle/backend/docker/egress_proxy.py → repo root.
_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)


def egress_proxy_container_name(slug: str) -> str:
    return f"claude-bottle-egress-proxy-{slug}"


def egress_proxy_url() -> str:
    """Base URL the agent will dial via HTTP_PROXY (chunk 2). Stable
    across bottles because the sidecar attaches `--network-alias
    egress-proxy` on the internal network; the container name (which
    carries the slug) is not referenced by agent-side config."""
    return f"http://{EGRESS_PROXY_HOSTNAME}:{EGRESS_PROXY_PORT}"


def build_egress_proxy_image() -> None:
    """Build the egress-proxy image from `Dockerfile.egress-proxy`.
    Called by `DockerEgressProxy.start`; exposed at module level so
    integration tests can build it without running the full launch
    pipeline."""
    docker_mod.build_image(
        EGRESS_PROXY_IMAGE, _REPO_DIR, dockerfile=EGRESS_PROXY_DOCKERFILE,
    )


class DockerEgressProxy(EgressProxy):
    """Brings the egress-proxy sidecar up and down via Docker."""

    def start(self, plan: EgressProxyPlan) -> str:
        """Boot the egress-proxy sidecar:
          1. Resolve every host TokenRef env var into a concrete
             value. Fails early if any are unset.
          2. Build the egress-proxy image (no-op when cache is hot).
          3. `docker create` on the internal network with
             `--network-alias egress-proxy` and one `-e EGRESS_PROXY_TOKEN_N`
             flag per token slot. The values arrive via subprocess env, so
             they never land on argv.
          4. `docker cp` the routes.yaml into the container.
          5. Attach to the per-agent egress network so the proxy can
             reach pipelock (chunk 2 turns this into the pipelock leg
             via HTTPS_PROXY).
          6. `docker start`.
        Returns the container name (the target passed to `.stop`)."""
        if not plan.routes:
            die("DockerEgressProxy.start called with no routes; caller should skip")
        if not plan.internal_network or not plan.egress_network:
            die(
                "DockerEgressProxy.start: internal_network / egress_network must be "
                "populated on the plan before start"
            )
        if not plan.routes_path.is_file():
            die(
                f"egress-proxy routes file missing at {plan.routes_path}; "
                f"EgressProxy.prepare must run first"
            )

        # Resolve host env vars into concrete values. Must happen at
        # start time (not prepare) — the values flow into the sidecar's
        # environ via subprocess env. The plan never holds them.
        token_values = egress_proxy_resolve_token_values(
            plan.token_env_map, dict(os.environ),
        )

        build_egress_proxy_image()

        name = egress_proxy_container_name(plan.slug)
        info(f"starting egress-proxy sidecar {name} on network {plan.internal_network}")

        create_args = [
            "docker", "create",
            "--name", name,
            "--network", plan.internal_network,
            "--network-alias", EGRESS_PROXY_HOSTNAME,
        ]
        if plan.pipelock_proxy_url:
            # Route egress-proxy's outbound HTTPS through pipelock so
            # the egress allowlist + DLP body scanner apply to its
            # traffic on the egress-proxy → upstream leg. Wiring lands
            # in chunk 2.
            create_args.extend([
                "-e", f"HTTPS_PROXY={plan.pipelock_proxy_url}",
                "-e", f"HTTP_PROXY={plan.pipelock_proxy_url}",
                "-e", "NO_PROXY=localhost,127.0.0.1",
            ])
        # One -e flag per token slot; values arrive via subprocess env.
        # docker create with `-e NAME` (no =VALUE) reads NAME from the
        # current process env at create time. We pass `env=child_env`
        # to subprocess.run so the value comes from token_values, not
        # the host's os.environ directly — keeps the resolver in one
        # place and lets egress_proxy_resolve_token_values surface
        # missing-env errors with a clear hint.
        for token_env in sorted(plan.token_env_map.keys()):
            create_args.extend(["-e", token_env])
        create_args.append(EGRESS_PROXY_IMAGE)

        child_env: dict[str, str] = {**os.environ, **token_values}

        create_result = subprocess.run(
            create_args, capture_output=True, text=True, env=child_env, check=False,
        )
        if create_result.returncode != 0:
            die(
                f"failed to create egress-proxy sidecar {name}: "
                f"{create_result.stderr.strip()}"
            )

        cp_result = subprocess.run(
            ["docker", "cp", str(plan.routes_path),
             f"{name}:{EGRESS_PROXY_ROUTES_IN_CONTAINER}"],
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
                f"failed to copy routes.yaml into {name}: "
                f"{cp_result.stderr.strip()}"
            )

        connect_result = subprocess.run(
            ["docker", "network", "connect", plan.egress_network, name],
            capture_output=True, text=True, check=False,
        )
        if connect_result.returncode != 0:
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            die(
                f"failed to attach egress-proxy sidecar {name} to egress network "
                f"{plan.egress_network}: {connect_result.stderr.strip()}"
            )

        start_result = subprocess.run(
            ["docker", "start", name], capture_output=True, text=True, check=False,
        )
        if start_result.returncode != 0:
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            die(
                f"failed to start egress-proxy sidecar {name}: "
                f"{start_result.stderr.strip()}"
            )

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
                    f"failed to remove egress-proxy sidecar {target}; "
                    f"clean up with 'docker rm -f {target}'"
                )
