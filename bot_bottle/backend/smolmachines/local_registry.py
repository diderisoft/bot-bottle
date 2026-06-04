"""Ephemeral local OCI registry for the smolmachines agent-image
conversion path (PRD 0023 chunk 4c).

`smolvm pack create --image <ref>` only accepts OCI registry refs
— it can't read the local docker daemon's image cache, an OCI
layout directory, or a `docker save` tarball. To convert the
agent's Dockerfile-built image into a `.smolmachine` artifact we
spin up a short-lived `registry:2.8.3` container alongside a
`crane` helper container on a private docker network, push via
`crane push --insecure <tarball> <registry-container>:5000/...`,
and let smolvm pull from the registry's published host port. The
network + both containers are torn down after the pack completes.

Why this two-container dance instead of plain `docker push`:
- Docker Desktop's daemon runs in its own Linux VM, so its
  `localhost` is not the host's loopback. A registry bound to
  the host's 127.0.0.1 is unreachable from the daemon side.
- `host.docker.internal` is reachable from the daemon but isn't
  in Docker's default insecure-registries CIDRs (only `::1/128`
  and `127.0.0.0/8` are), so `docker push` to it tries HTTPS,
  hits a plain-HTTP registry, and dies with
  `http: server gave HTTP response to HTTPS client`. Adding
  `host.docker.internal` to daemon.json works but is a one-time
  manual step the user has to do in Docker Desktop's UI.
- Going through a docker network sidesteps the host-vs-daemon
  loopback mismatch (crane and registry containers see each
  other on the network) AND the HTTPS preference (crane has an
  `--insecure` flag that forces plain HTTP).

The registry is also published on a random host port so smolvm
— a host process — can pull from `localhost:<port>` via Docker's
port-forward. smolvm's bundled crane auto-falls-back to HTTP for
localhost addresses, so no insecure-registries config is needed
on that side either."""

from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Generator

from ...log import die


# registry:2.8.3, pinned by digest. Same env-override pattern as the
# pipelock image pin in bot_bottle/backend/docker/pipelock.py.
REGISTRY_IMAGE = os.environ.get(
    "BOT_BOTTLE_REGISTRY_IMAGE",
    "registry@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373",
)


# gcr.io/go-containerregistry/crane:latest, pinned by digest. ~10MB,
# stable upstream from Google; we only invoke `crane push --insecure`
# against a localhost-equivalent registry, so the trust surface is
# narrow.
CRANE_IMAGE = os.environ.get(
    "BOT_BOTTLE_CRANE_IMAGE",
    (
        "gcr.io/go-containerregistry/crane@sha256:"
        "0ae17ecb34315aa7cbff28f6eddee3b7adae0b2f90101260d990804db1eb0084"
    ),
)


# Internal port the registry binds to inside its container — fixed
# by the registry:2 image. The host-side mapping is random.
_REGISTRY_CONTAINER_PORT = "5000"


# How long to wait for the registry's HTTP layer to bind before
# giving up. Two seconds is empirically enough; 10s leaves headroom
# for slow CI runners without making the failure mode chatty.
_READY_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class RegistryHandle:
    """Everything callers need to push to + pull from the ephemeral
    registry.

    `network` is the per-session docker network — a `crane push`
    container has to join it to reach the registry by name.
    `push_endpoint` is the `<host>:<port>` form to embed in image
    refs given to the crane push container (resolves via docker
    network DNS). `pull_endpoint` is the `<host>:<port>` form a
    host process (smolvm) uses; the registry's host port mapping
    backs this."""

    network: str
    push_endpoint: str
    pull_endpoint: str


@contextmanager
def ephemeral_registry() -> Generator[RegistryHandle, None, None]:
    """Bring up a per-session docker network + a `registry:2.8.3`
    container on it (published on a random host port), yield a
    `RegistryHandle`, force-remove both on exit.

    The container is started with `--rm` so a clean exit cleans up
    on its own; the `finally` block force-removes on abnormal exit
    (the calling process crashes between yield and close)."""
    session_id = uuid.uuid4().hex[:12]
    network = f"bot-bottle-registry-net-{session_id}"
    registry_name = f"bot-bottle-registry-{session_id}"

    subprocess.run(
        ["docker", "network", "create", network],
        check=True,
        capture_output=True,
    )
    try:
        subprocess.run(
            [
                "docker", "run", "-d", "--rm",
                "--name", registry_name,
                "--network", network,
                # `-p :5000` (no IP prefix) binds the container's
                # port 5000 on a random host port across all
                # interfaces. The host side reaches the registry
                # via this port — smolvm's `pack create` pulls from
                # `localhost:<port>` and the docker port-forward
                # routes there.
                "-p", _REGISTRY_CONTAINER_PORT,
                REGISTRY_IMAGE,
            ],
            check=True,
            capture_output=True,
        )
        try:
            port = _host_port(registry_name)
            _wait_ready(port)
            yield RegistryHandle(
                network=network,
                push_endpoint=f"{registry_name}:{_REGISTRY_CONTAINER_PORT}",
                pull_endpoint=f"localhost:{port}",
            )
        finally:
            subprocess.run(
                ["docker", "rm", "-f", registry_name],
                check=False,
                capture_output=True,
            )
    finally:
        subprocess.run(
            ["docker", "network", "rm", network],
            check=False,
            capture_output=True,
        )


def crane_push_tarball(handle: RegistryHandle, tarball_path: str, ref: str) -> None:
    """Run `crane push --insecure <tarball> <ref>` inside a one-shot
    container on the registry's docker network. `ref` should
    reference the registry by `handle.push_endpoint` so the crane
    container resolves it via docker network DNS.

    Doesn't go through `docker push` to avoid the Docker-Desktop
    daemon's HTTPS preference for non-loopback hostnames — crane's
    `--insecure` flag forces plain HTTP, which is what the
    registry container speaks."""
    r = subprocess.run(
        [
            "docker", "run", "--rm",
            "--network", handle.network,
            "-v", f"{tarball_path}:/img.tar:ro",
            CRANE_IMAGE,
            "push", "--insecure", "/img.tar", ref,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        die(
            f"crane push of {tarball_path!r} to {ref!r} failed: "
            f"{(r.stderr or r.stdout or '').strip() or '<no output>'}"
        )


def _host_port(name: str) -> int:
    """Resolve the host-side port docker mapped to the registry's
    container port. `docker port <name> 5000/tcp` returns one or
    more `host:port` lines (one per address family) — we take the
    first."""
    r = subprocess.run(
        ["docker", "port", name, f"{_REGISTRY_CONTAINER_PORT}/tcp"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        die(
            f"docker port {name} {_REGISTRY_CONTAINER_PORT}/tcp failed: "
            f"{(r.stderr or '').strip() or '<no stderr>'}"
        )
    # `0.0.0.0:54321\n[::]:54321\n` — split on the last colon to
    # handle either IPv4 or IPv6 host syntax.
    line = (r.stdout or "").splitlines()[0].strip()
    _, _, port_str = line.rpartition(":")
    try:
        return int(port_str)
    except ValueError:
        die(f"unexpected `docker port` output: {line!r}")


def _wait_ready(port: int) -> None:
    """Block until the registry's HTTP layer accepts a TCP
    connection on `127.0.0.1:<port>`, or `_READY_TIMEOUT_S`
    elapses.

    A successful TCP connect is sufficient — registry:2.8.3 binds
    after it's ready to serve `/v2/` requests, so the push that
    follows will land on a working server. We probe loopback
    specifically (not via the docker network) because this helper
    runs on the host."""
    deadline = time.monotonic() + _READY_TIMEOUT_S
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError as e:
            last_err = e
            time.sleep(0.1)
    die(
        f"local registry on 127.0.0.1:{port} did not accept "
        f"connections within {_READY_TIMEOUT_S:.0f}s "
        f"(last error: {last_err})"
    )
