"""Ephemeral local OCI registry for the smolmachines agent-image
conversion path (PRD 0023 chunk 4c).

`smolvm pack create --image <ref>` only accepts registry refs — it
can't read the local docker daemon's image cache, an OCI layout
directory, or a `docker save` tarball. To convert the agent's
Dockerfile-built image into a `.smolmachine` artifact we run a
short-lived `registry:2.8.3` container on `127.0.0.1:<random>`,
push the locally-tagged image into it, and let smolvm pull from
there. The registry container is torn down as soon as the pack
completes.

Loopback-only bind + the host's docker layer cache mean the round
trip is fast (~5s) and there's no exposed surface on the LAN."""

from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from contextlib import contextmanager
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


@contextmanager
def ephemeral_registry() -> Iterator[int]:
    """Bring up a `registry:2.8.3` container on a random loopback
    port, yield the port, force-remove the container on exit.

    The container is started with `--rm` so a clean exit cleans up
    on its own; the `finally` block force-removes on abnormal exit
    (the calling process crashes between yield and close)."""
    name = f"claude-bottle-registry-{uuid.uuid4().hex[:12]}"
    subprocess.run(
        [
            "docker", "run", "-d", "--rm",
            "--name", name,
            # `127.0.0.1::5000` = bind to loopback, pick a random host
            # port. No LAN exposure; the container hangs around just
            # long enough for one push + one pack-create.
            "-p", "127.0.0.1::5000",
            REGISTRY_IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    try:
        port = _host_port(name)
        _wait_ready(port)
        yield port
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name],
            check=False,
            capture_output=True,
        )


def _host_port(name: str) -> int:
    """Resolve the host-side port docker mapped to the registry's
    container port 5000. `docker port <name> 5000/tcp` returns one or
    more `host:port` lines; the loopback-only -p binding ensures we
    get exactly `127.0.0.1:<port>`."""
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
    # `127.0.0.1:54321\n` — split on the last colon to handle the
    # `host:port` shape without parsing IP literals.
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
    follows will land on a working server."""
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
