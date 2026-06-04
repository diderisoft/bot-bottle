"""Docker-side pipelock helpers: image pin, container naming, and
the one-shot `pipelock tls init` host-side CA mint. The
prepare-time YAML rendering itself lives on the platform-neutral
`PipelockProxy` ABC — backends instantiate it directly.

The per-container `.start()` / `.stop()` lifecycle was deleted in
PRD 0024 chunk 3; compose-up owns the container lifecycle (PRD
0018) and the bundle path (PRD 0024) collapses pipelock + egress
+ git-gate + supervise into one container."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ...log import die


# Pipelock image, pinned by digest. The digest is the multi-arch image
# index for ghcr.io/luckypipewrench/pipelock:2.3.0.
PIPELOCK_IMAGE = os.environ.get(
    "BOT_BOTTLE_PIPELOCK_IMAGE",
    "ghcr.io/luckypipewrench/pipelock@sha256:"
    "3b1a39417b98406ddc5dc2d8fcb42865ddc0c68a43d355db55f0f8cb06bc6de9",
)

# Listening port for pipelock's forward proxy.
PIPELOCK_PORT = os.environ.get("BOT_BOTTLE_PIPELOCK_PORT", "8888")


# The URL egress dials for its upstream HTTPS_PROXY. egress and pipelock
# share the same container's network namespace inside the sidecar bundle, so
# loopback reaches pipelock directly — no docker DNS aliases involved.
BUNDLE_LOCAL_PIPELOCK_URL = f"http://127.0.0.1:{PIPELOCK_PORT}"


def pipelock_tls_init(stage_dir: Path) -> tuple[Path, Path]:
    """Generate a fresh per-bottle CA via a one-shot pipelock container.

    Runs `pipelock tls init` against a host-mounted scratch dir, leaving
    `ca.pem` (public cert, mode 600) and `ca-key.pem` (private key, mode
    600) under `<stage_dir>/pipelock-ca/`. Returns the two host paths.

    The image is pinned (same digest the running sidecar uses) so the
    generated CA matches what the sidecar expects. Output is owned by
    whatever UID the one-shot ran as; the compose renderer's
    bind-mounts pin the files in place at runtime, so ownership
    inside the running sidecar (root in pipelock's distroless image)
    is independent."""
    work = stage_dir / "pipelock-ca"
    work.mkdir(exist_ok=True)
    result = subprocess.run(
        ["docker", "run", "--rm",
         "-v", f"{work}:/h",
         "-e", "PIPELOCK_HOME=/h",
         PIPELOCK_IMAGE, "tls", "init"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        die(f"pipelock tls init failed: {result.stderr.strip()}")
    cert = work / "ca.pem"
    key = work / "ca-key.pem"
    if not cert.is_file() or not key.is_file():
        die(f"pipelock tls init did not produce ca files in {work}")
    # Explicit perms in case a future pipelock release changes
    # defaults. Pipelock runs as root in its distroless image and
    # bind-mounts work with 0o600 (root reads everything); the key
    # has no reason to be readable to anyone else on the host.
    key.chmod(0o600)
    cert.chmod(0o644)
    return (cert, key)
