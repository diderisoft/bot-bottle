"""DockerSupervise — the Docker-specific lifecycle for the per-bottle
supervise sidecar (PRD 0013). Inherits the platform-agnostic prepare
step (queue dir + current-config staging) from `Supervise`."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ...log import die, info, warn
from ...supervise import (
    QUEUE_DIR_IN_CONTAINER,
    SUPERVISE_HOSTNAME,
    SUPERVISE_PORT,
    Supervise,
    SupervisePlan,
)
from . import util as docker_mod


SUPERVISE_IMAGE = os.environ.get(
    "CLAUDE_BOTTLE_SUPERVISE_IMAGE",
    "claude-bottle-supervise:latest",
)

SUPERVISE_DOCKERFILE = "Dockerfile.supervise"

_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)


def supervise_container_name(slug: str) -> str:
    return f"claude-bottle-supervise-{slug}"


def supervise_url() -> str:
    """Base URL the agent's MCP client dials. Stable across bottles
    because the sidecar attaches `--network-alias supervise` on the
    internal network."""
    return f"http://{SUPERVISE_HOSTNAME}:{SUPERVISE_PORT}"


def build_supervise_image() -> None:
    """Build the supervise image from `Dockerfile.supervise`. Called
    by `DockerSupervise.start`; exposed at module level so tests can
    build it without running the full launch pipeline."""
    docker_mod.build_image(SUPERVISE_IMAGE, _REPO_DIR, dockerfile=SUPERVISE_DOCKERFILE)


class DockerSupervise(Supervise):
    """Brings the supervise sidecar up and down via Docker."""

    def start(self, plan: SupervisePlan) -> str:
        """Boot the supervise sidecar:
          1. Build the supervise image (no-op when cache is hot).
          2. `docker create` on the internal network with
             `--network-alias supervise` and SUPERVISE_BOTTLE_SLUG in
             the environ.
          3. Bind-mount the host queue dir at /run/supervise/queue.
          4. `docker start`.
        No egress network — the supervise sidecar does not make
        outbound calls. Returns the container name."""
        if not plan.internal_network:
            die("DockerSupervise.start: plan.internal_network must be set before start")
        if not plan.queue_dir.is_dir():
            die(
                f"DockerSupervise.start: queue dir missing at {plan.queue_dir}; "
                f"Supervise.prepare must run first"
            )

        build_supervise_image()

        name = supervise_container_name(plan.slug)
        info(f"starting supervise sidecar {name} on network {plan.internal_network}")

        create_args = [
            "docker", "create",
            "--name", name,
            "--network", plan.internal_network,
            "--network-alias", SUPERVISE_HOSTNAME,
            "-e", f"SUPERVISE_BOTTLE_SLUG={plan.slug}",
            "-e", f"SUPERVISE_QUEUE_DIR={QUEUE_DIR_IN_CONTAINER}",
            "-e", f"SUPERVISE_PORT={SUPERVISE_PORT}",
            "-v", f"{plan.queue_dir}:{QUEUE_DIR_IN_CONTAINER}",
            SUPERVISE_IMAGE,
        ]

        create_result = subprocess.run(
            create_args, capture_output=True, text=True, check=False,
        )
        if create_result.returncode != 0:
            die(
                f"failed to create supervise sidecar {name}: "
                f"{create_result.stderr.strip()}"
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
                f"failed to start supervise sidecar {name}: "
                f"{start_result.stderr.strip()}"
            )

        return name

    def stop(self, target: str) -> None:
        """Idempotent: missing container is success."""
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
                    f"failed to remove supervise sidecar {target}; "
                    f"clean up with 'docker rm -f {target}'"
                )
