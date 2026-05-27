"""Per-bottle sidecar bundle bringup for the smolmachines backend
(PRD 0023).

Two docker resources per bottle live here:

  - **A dedicated bridge network**, subnet derived from the slug.
    The bundle container gets a pinned IP at `<subnet>.2` so the
    smolvm guest's TSI allowlist (`<bundle-ip>/32`) has a stable
    target. Without pinning, we'd have to inspect the container's
    assigned IP after start and feed it back into the Smolfile
    — a race we can sidestep with `--ip`.

  - **The bundle container itself**, running the PRD 0024 bundle
    image (`claude-bottle-sidecars:latest` by default). Same
    image, same daemons, same daemon-private env / bind-mounts
    as the docker backend.

This module ships the lifecycle primitives only — create
network, start bundle, stop bundle, remove network — wrapped
around `subprocess.run(["docker", ...])`. Wiring them into the
launch flow + populating the `BundleLaunchSpec` from the inner
Plans (PipelockProxyPlan, EgressPlan, …) lands in chunk 2d."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from ...log import die, warn
from ..docker.sidecar_bundle import SIDECAR_BUNDLE_IMAGE


def bundle_network_name(slug: str) -> str:
    """`claude-bottle-bundle-<slug>` — distinct from the docker
    backend's `claude-bottle-net-<slug>` so a smolmachines bottle
    and a docker bottle for the same agent don't collide on
    network name."""
    return f"claude-bottle-bundle-{slug}"


def bundle_container_name(slug: str) -> str:
    """`claude-bottle-sidecars-<slug>` — same name shape the docker
    backend uses for the bundle (PRD 0024 chunk 5). The dashboard's
    prefix-based discovery covers both backends with one filter."""
    return f"claude-bottle-sidecars-{slug}"


@dataclass(frozen=True)
class BundleLaunchSpec:
    """Everything `start_bundle` needs to bring up one bundle
    container. Populated by chunk-2d's launch flow from the inner
    Plans the prepare step already produces."""

    slug: str
    network_name: str
    subnet: str
    gateway: str
    bundle_ip: str
    image: str = SIDECAR_BUNDLE_IMAGE
    # Daemon subset CSV for CLAUDE_BOTTLE_SIDECAR_DAEMONS. The
    # supervisor inside the bundle reads it to skip
    # bottle-irrelevant daemons (e.g. supervise=False bottles).
    daemons_csv: str = "egress,pipelock"
    # Plain "KEY=VALUE" strings + "KEY" bare names (the bare-name
    # form inherits the value from the docker-run subprocess env,
    # matching the docker backend's compose-up secret-forwarding
    # pattern).
    environment: Sequence[str] = field(default_factory=tuple)
    # (host_path, container_path, read_only) bind mounts.
    volumes: Sequence[tuple[str, str, bool]] = field(default_factory=tuple)


def create_bundle_network(network_name: str, subnet: str, gateway: str) -> None:
    """`docker network create` with an explicit subnet + gateway
    so the bundle's `--ip` lands on the address the Smolfile's
    TSI allowlist points at. Idempotent on the caller's side —
    `start_bundle` catches the "network exists" error and treats
    it as success (chunk-2d teardown is paired with each create).
    """
    result = subprocess.run(
        ["docker", "network", "create",
         "--subnet", subnet, "--gateway", gateway,
         network_name],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        # Already-exists is fine on a resume path; everything else
        # is fatal — the bundle won't have an addressable network.
        if "already exists" in (result.stderr or "").lower():
            return
        die(
            f"docker network create {network_name} failed: "
            f"{(result.stderr or '').strip()}"
        )


def remove_bundle_network(network_name: str) -> None:
    """Idempotent: a missing network returns success."""
    result = subprocess.run(
        ["docker", "network", "rm", network_name],
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 0:
        return
    if "no such network" in (result.stderr or "").lower():
        return
    # Network with attached containers is the common non-fatal
    # case during a partial teardown — warn but don't die.
    warn(
        f"docker network rm {network_name} failed: "
        f"{(result.stderr or '').strip()}"
    )


def start_bundle(spec: BundleLaunchSpec, *,
                 env: dict[str, str] | None = None) -> None:
    """Bring the bundle container up on the per-bottle bridge with
    the pinned IP. Argv is built deterministically from `spec`;
    `env` is the host subprocess env (forwarded values for any
    bare-name entries in `spec.environment`)."""
    container = bundle_container_name(spec.slug)
    argv = [
        "docker", "run",
        "--name", container,
        "--detach",
        "--rm",
        "--network", spec.network_name,
        "--ip", spec.bundle_ip,
        "-e", f"CLAUDE_BOTTLE_SIDECAR_DAEMONS={spec.daemons_csv}",
    ]
    for entry in spec.environment:
        argv += ["-e", entry]
    for host_path, container_path, read_only in spec.volumes:
        suffix = ":ro" if read_only else ""
        argv += ["-v", f"{host_path}:{container_path}{suffix}"]
    argv.append(spec.image)
    result = subprocess.run(
        argv, capture_output=True, text=True,
        env=dict(env) if env is not None else None, check=False,
    )
    if result.returncode != 0:
        die(
            f"docker run for bundle {container} failed: "
            f"{(result.stderr or '').strip()}"
        )


def stop_bundle(slug: str) -> None:
    """Idempotent: a missing container returns success."""
    container = bundle_container_name(slug)
    result = subprocess.run(
        ["docker", "rm", "-f", container],
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 0:
        return
    if "no such container" in (result.stderr or "").lower():
        return
    warn(
        f"docker rm -f {container} failed: "
        f"{(result.stderr or '').strip()}"
    )
