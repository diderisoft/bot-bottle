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
    image (`bot-bottle-sidecars:latest` by default). Same
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
from ..docker import util as docker_mod
from ..docker.sidecar_bundle import (
    SIDECAR_BUNDLE_DOCKERFILE,
    SIDECAR_BUNDLE_IMAGE,
)


_REPO_DIR = str(Path(__file__).resolve().parent.parent.parent.parent)


def bundle_network_name(slug: str) -> str:
    """`bot-bottle-bundle-<slug>` — distinct from the docker
    backend's `bot-bottle-net-<slug>` so a smolmachines bottle
    and a docker bottle for the same agent don't collide on
    network name."""
    return f"bot-bottle-bundle-{slug}"


def bundle_container_name(slug: str) -> str:
    """`bot-bottle-sidecars-<slug>` — same name shape the docker
    backend uses for the bundle (PRD 0024 chunk 5). The dashboard's
    prefix-based discovery covers both backends with one filter."""
    return f"bot-bottle-sidecars-{slug}"


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
    # Daemon subset CSV for BOT_BOTTLE_SIDECAR_DAEMONS. The
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
    # Container ports to publish on `publish_host_ip`, random
    # host-side port per entry. The smolvm guest's TSI talks via
    # macOS networking, so docker container IPs (192.168.x.x in
    # the daemon's bridge) aren't directly reachable from the
    # guest — host-loopback port-forwards are. Egress's port
    # is bundle-internal and never published.
    ports_to_publish: Sequence[int] = field(default_factory=tuple)
    # Loopback IP to bind published ports against. Per-bottle
    # loopback aliases (`127.0.0.16` etc., added via sudo
    # ifconfig lo0 alias) narrow the TSI allowlist so a bottle
    # can't reach other bottles' (or other host services') ports
    # via 127.0.0.1.
    publish_host_ip: str = "127.0.0.1"


def ensure_bundle_image(image: str = SIDECAR_BUNDLE_IMAGE) -> None:
    """Build the sidecar bundle image before `docker run`.

    The Docker backend gets this for free from compose's `build:`
    stanza. smolmachines starts the bundle with plain `docker run`,
    so without an explicit build a first launch tries to pull the
    local-only `bot-bottle-sidecars:latest` tag from a registry.
    """
    docker_mod.build_image(
        image,
        _REPO_DIR,
        dockerfile=SIDECAR_BUNDLE_DOCKERFILE,
    )


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
        "-e", f"BOT_BOTTLE_SIDECAR_DAEMONS={spec.daemons_csv}",
    ]
    for entry in spec.environment:
        argv += ["-e", entry]
    for host_path, container_path, read_only in spec.volumes:
        suffix = ":ro" if read_only else ""
        argv += ["-v", f"{host_path}:{container_path}{suffix}"]
    # Loopback-only host port-forwards — the smolvm guest's TSI
    # uses macOS networking, and macOS loopback is the only host
    # surface that round-trips into Docker Desktop's daemon VM.
    # Binds to the per-bottle alias so TSI's IP-only allowlist
    # narrows reachability to this bottle's bundle only.
    for port in spec.ports_to_publish:
        argv += ["-p", f"{spec.publish_host_ip}::{port}"]
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


def bundle_host_port(
    slug: str, container_port: int, *, host_ip: str = "127.0.0.1",
) -> int:
    """`docker port <bundle> <container_port>/tcp` → the random
    host-side port docker assigned for the binding on `host_ip`.
    Called after `start_bundle` on each container port listed in
    `BundleLaunchSpec.ports_to_publish` so the launch step can
    build the agent's HTTPS_PROXY / GIT_GATE / SUPERVISE URLs in
    `<host_ip>:<host port>` form."""
    container = bundle_container_name(slug)
    result = subprocess.run(
        ["docker", "port", container, f"{container_port}/tcp"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        die(
            f"docker port {container} {container_port}/tcp failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}"
        )
    # Each line looks like `127.0.0.16:54321` — one per address
    # family / host IP. Match on the expected host_ip prefix so
    # bottles bound to per-bottle aliases pick the right line.
    for raw in (result.stdout or "").splitlines():
        line = raw.strip()
        if line.startswith(f"{host_ip}:"):
            _, _, port_str = line.rpartition(":")
            try:
                return int(port_str)
            except ValueError:
                die(f"unexpected `docker port` output: {line!r}")
    die(
        f"no port mapping on {host_ip} for {container} "
        f"{container_port}/tcp; got: {(result.stdout or '').strip()!r}"
    )
    return -1  # unreachable; die() never returns


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
