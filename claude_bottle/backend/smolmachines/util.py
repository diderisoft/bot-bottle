"""Slug / preflight helpers for the smolmachines backend (PRD 0023).

Backend-specific utilities the prepare/launch flow reaches for.
Kept in its own module so the renderers can be unit-tested without
importing the docker subprocess paths."""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
from contextlib import closing

from ...log import die


def smolmachines_preflight() -> None:
    """Ensure the binaries the smolmachines backend shells out to are
    on PATH. Called from the backend's `_resolve_plan` before any
    file writes; gives the operator a clear pointer rather than a
    cryptic FileNotFoundError later.

    `smolvm` drives the libkrun microVM lifecycle. `gvproxy`
    (gvisor-tap-vsock) is the userspace TCP/IP stack the guest's
    virtio-net device hooks into."""
    missing: list[tuple[str, str]] = []
    if shutil.which("smolvm") is None:
        missing.append((
            "smolvm",
            "brew install smolmachines-dev/smolmachines/smolmachines  "
            "# or build from https://smolmachines.com/",
        ))
    if shutil.which("gvproxy") is None:
        missing.append((
            "gvproxy",
            "go install "
            "github.com/containers/gvisor-tap-vsock/cmd/gvproxy@latest  "
            "# requires Go + ensure $GOPATH/bin on PATH",
        ))
    if not missing:
        return
    lines = [
        f"CLAUDE_BOTTLE_BACKEND=smolmachines requires the following "
        f"binar{'y' if len(missing) == 1 else 'ies'} on PATH:",
    ]
    for name, install in missing:
        lines.append(f"  - {name}    install: {install}")
    die("\n".join(lines))


def smolmachines_gvproxy_subnet(slug: str) -> tuple[str, str]:
    """Derive a per-bottle subnet + gateway IP from the slug.

    Returns `(subnet_cidr, gateway_ip)`. The third octet comes from
    SHA-256 of the slug mod 254 (1..254, skipping 0 and 255 + the
    127.x.x.x range) — collision-free for any reasonable concurrent-
    bottle count and stable across `start` / `resume` of the same
    bottle. Bottles that DO collide would fail at gvproxy bringup;
    `launch.py` (chunk 2) detects the conflict and surfaces a clear
    error rather than silently reusing another bottle's gateway."""
    digest = hashlib.sha256(slug.encode("utf-8")).digest()
    octet = (digest[0] % 254) + 1
    # Skip docker-default 17 to dodge the usual second-bridge
    # collision. Operators with docker's default bridge at
    # 172.17.x.x — common Linux setup — would otherwise see a
    # collision the moment they run a parallel docker bottle.
    if octet == 17:
        octet = 18
    subnet = f"192.168.{octet}.0/24"
    gateway = f"192.168.{octet}.1"
    return subnet, gateway


def allocate_loopback_port() -> int:
    """Bind / release dance to grab a free TCP port on 127.0.0.1.

    Race window: a parallel allocator could grab the same port
    between our close and the bundle's bind. The window is small
    enough that chunk 1 doesn't address it; chunk 3 will add a
    retry loop if it shows up in practice."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
