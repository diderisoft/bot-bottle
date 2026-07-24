"""Docker launch broker (PRD 0070) — the first *real* LaunchBroker.

On a verified launch request it starts a Docker container; on teardown it
removes it. This proves the orchestrator -> backend seam on the cheapest
backend (the gateway is already containers). Only the request's
static ids/flags reach `docker`, so nothing free-form crosses the boundary.

Slice 3 launches a single container from the request's `image_ref`, named
after the bottle id and labelled for cleanup. Wiring the full agent +
gateway (networks, mounts, the consolidated gateway) is a later
slice — this is the seam, not the finished launcher.
"""

from __future__ import annotations

import subprocess

from ..backend.docker.util import run_docker
from .broker import LaunchBroker, LaunchRequest

CONTAINER_PREFIX = "bot-bottle-orch-"
BOTTLE_ID_LABEL = "bot-bottle-bottle-id"


class DockerBrokerError(Exception):
    """A brokered docker launch/teardown failed (non-zero `docker` exit)."""


def container_name(bottle_id: str) -> str:
    """Deterministic container name for a bottle."""
    return f"{CONTAINER_PREFIX}{bottle_id}"


def run_argv(req: LaunchRequest) -> list[str]:
    """`docker run` argv for a launch request — built only from its static
    fields, so it can't be coerced into an arbitrary command."""
    return [
        "docker", "run", "--detach",
        "--name", container_name(req.bottle_id),
        "--label", f"{BOTTLE_ID_LABEL}={req.bottle_id}",
        req.image_ref,
    ]


def rm_argv(req: LaunchRequest) -> list[str]:
    """`docker rm --force` argv to tear a bottle's container down."""
    return ["docker", "rm", "--force", container_name(req.bottle_id)]


class DockerBroker(LaunchBroker):
    """A `LaunchBroker` that runs / removes a Docker container per bottle.
    Provenance + schema verification are inherited from `LaunchBroker`."""

    def _docker(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return run_docker(argv)

    def _launch(self, req: LaunchRequest) -> None:
        if not req.image_ref:
            raise DockerBrokerError(f"launch request for {req.bottle_id} has no image_ref")
        proc = self._docker(run_argv(req))
        if proc.returncode != 0:
            raise DockerBrokerError(
                f"docker run failed for {req.bottle_id}: {proc.stderr.strip()}"
            )

    def _teardown(self, req: LaunchRequest) -> None:
        proc = self._docker(rm_argv(req))
        # Idempotent: an already-absent container is a successful teardown.
        if proc.returncode != 0 and "No such container" not in proc.stderr:
            raise DockerBrokerError(
                f"docker rm failed for {req.bottle_id}: {proc.stderr.strip()}"
            )


__all__ = [
    "DockerBroker",
    "DockerBrokerError",
    "container_name",
    "run_argv",
    "rm_argv",
    "CONTAINER_PREFIX",
    "BOTTLE_ID_LABEL",
]
