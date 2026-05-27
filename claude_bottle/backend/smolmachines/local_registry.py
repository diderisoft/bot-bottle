"""Ephemeral local OCI registry for the smolmachines agent-image
conversion path (PRD 0023 chunk 4c).

`smolvm pack create --image <ref>` only accepts registry refs — it
can't read the local docker daemon's image cache, an OCI layout
directory, or a `docker save` tarball. To convert the agent's
Dockerfile-built image into a `.smolmachine` artifact we run a
short-lived `registry:2.8.3` container, push the locally-tagged
image into it, and let smolvm pull from there. The registry
container is torn down as soon as the pack completes.

Two routing hostnames, one registry container. On Docker Desktop
(macOS/Windows) the docker daemon runs inside its own Linux VM,
so its `localhost` is *not* the host's loopback — a registry
bound to `127.0.0.1::<port>` on the host is unreachable from the
daemon side, and `docker push` fails with `context deadline
exceeded`. The fix: bind to all interfaces so both routes work,
and yield two refs:

  - `daemon_endpoint`: how the docker CLI/daemon dials the
    registry (`host.docker.internal:<port>` on Docker Desktop,
    `localhost:<port>` on a native Linux daemon that shares the
    host's network namespace).
  - `host_endpoint`: how `smolvm pack create` (a host process)
    dials the registry. Always `localhost:<port>` — the port
    binding includes loopback either way.

The registry stores images by repo+tag; the hostname in the ref
is just routing, so a push to `host.docker.internal:<port>/cb:abc`
and a pull of `localhost:<port>/cb:abc` hit the same stored
blob.

Trade-off: binding to all interfaces puts the registry on every
network interface briefly (~5-10s during prepare). The agent
image we push is built from the repo's public Dockerfile — no
secrets in it — and the user is on their own machine; the LAN
exposure window is short and the contents non-sensitive."""

from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from ...log import die


# registry:2.8.3, pinned by digest. Same env-override pattern as the
# pipelock image pin in claude_bottle/backend/docker/pipelock.py.
REGISTRY_IMAGE = os.environ.get(
    "CLAUDE_BOTTLE_REGISTRY_IMAGE",
    "registry@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373",
)


# How long to wait for the registry's HTTP layer to bind before
# giving up. Two seconds is empirically enough; bumping to 10s leaves
# headroom for slow CI runners without making the failure mode chatty.
_READY_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class RegistryEndpoints:
    """The two `<host>:<port>` strings to embed in image refs. They
    point at the same registry container; only the routing
    hostname differs."""

    daemon_endpoint: str
    host_endpoint: str


@contextmanager
def ephemeral_registry() -> Iterator[RegistryEndpoints]:
    """Bring up a `registry:2.8.3` container on a random host port,
    yield the daemon-side + host-side endpoints, force-remove the
    container on exit.

    The container is started with `--rm` so a clean exit cleans up
    on its own; the `finally` block force-removes on abnormal exit
    (the calling process crashes between yield and close)."""
    name = f"claude-bottle-registry-{uuid.uuid4().hex[:12]}"
    subprocess.run(
        [
            "docker", "run", "-d", "--rm",
            "--name", name,
            # `-p :5000` (no IP prefix) binds the container's port
            # 5000 on a random host port across all interfaces. The
            # registry container itself listens on 0.0.0.0:5000
            # internally; binding to all interfaces is necessary for
            # Docker Desktop's daemon to reach it via
            # host.docker.internal — a 127.0.0.1-only host binding
            # is invisible to a daemon running in its own VM.
            "-p", "5000",
            REGISTRY_IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    try:
        port = _host_port(name)
        _wait_ready(port)
        daemon_host = _daemon_side_hostname()
        yield RegistryEndpoints(
            daemon_endpoint=f"{daemon_host}:{port}",
            host_endpoint=f"localhost:{port}",
        )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name],
            check=False,
            capture_output=True,
        )


def _daemon_side_hostname() -> str:
    """Pick the hostname the docker daemon should use to dial the
    registry. On Docker Desktop the daemon runs in its own Linux
    VM and only sees the host via `host.docker.internal`; on
    native Linux the daemon shares the host's network namespace
    and `localhost` works.

    `docker info --format '{{.OperatingSystem}}'` returns
    `"Docker Desktop"` on macOS / Windows Desktop installs (and on
    Linux Desktop, which also uses a VM). Anything else (e.g.
    `"Debian GNU/Linux 12 (bookworm)"`) is a native daemon."""
    r = subprocess.run(
        ["docker", "info", "--format", "{{.OperatingSystem}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    operating_system = (r.stdout or "").strip()
    if operating_system == "Docker Desktop":
        return "host.docker.internal"
    return "localhost"


def _host_port(name: str) -> int:
    """Resolve the host-side port docker mapped to the registry's
    container port 5000. `docker port <name> 5000/tcp` returns one
    or more `host:port` lines (one per address family) — we take
    the first IPv4 line."""
    r = subprocess.run(
        ["docker", "port", name, "5000/tcp"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        die(
            f"docker port {name} 5000/tcp failed: "
            f"{(r.stderr or '').strip() or '<no stderr>'}"
        )
    # `0.0.0.0:54321\n[::]:54321\n` — take the first line, split
    # on the last colon to handle either IPv4 or IPv6 host syntax.
    line = (r.stdout or "").splitlines()[0].strip()
    _, _, port_str = line.rpartition(":")
    try:
        return int(port_str)
    except ValueError:
        die(f"unexpected `docker port` output: {line!r}")
        return -1  # unreachable; die() never returns


def _wait_ready(port: int) -> None:
    """Block until the registry's HTTP layer accepts a TCP connection
    on `127.0.0.1:<port>`, or `_READY_TIMEOUT_S` elapses.

    A successful TCP connect is sufficient — registry:2.8.3 binds
    after it's ready to serve `/v2/` requests, so the push that
    follows will land on a working server. We probe loopback
    specifically (not host.docker.internal) because this helper
    runs on the host, and 0.0.0.0-bound ports are reachable via
    127.0.0.1 too."""
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
