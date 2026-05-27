"""DockerPipelockProxy — the Docker-specific implementation of the
sidecar's `.prepare()` step + in-container CA path constants.
Inherits the platform-agnostic YAML-config generation from
PipelockProxy.

The per-container `.start()` / `.stop()` lifecycle was deleted in
PRD 0024 chunk 3 — compose-up owns the container lifecycle (PRD
0018) and the bundle path (PRD 0024) collapses pipelock + egress
+ git-gate + supervise into one container. What remains here is
the prepare-time YAML rendering + the CA path constants the
compose renderer reads."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ...log import die
from ...pipelock import PipelockProxy


# Pipelock image, pinned by digest. The digest is the multi-arch image
# index for ghcr.io/luckypipewrench/pipelock:2.3.0.
PIPELOCK_IMAGE = os.environ.get(
    "CLAUDE_BOTTLE_PIPELOCK_IMAGE",
    "ghcr.io/luckypipewrench/pipelock@sha256:3b1a39417b98406ddc5dc2d8fcb42865ddc0c68a43d355db55f0f8cb06bc6de9",
)

# Listening port for pipelock's forward proxy.
PIPELOCK_PORT = os.environ.get("CLAUDE_BOTTLE_PIPELOCK_PORT", "8888")

# In-container paths where the per-bottle CA cert + key land via
# the compose renderer's bind-mounts. Pipelock's rendered YAML
# references these paths under `tls_interception`.
PIPELOCK_CA_CERT_IN_CONTAINER = "/etc/pipelock-ca.pem"
PIPELOCK_CA_KEY_IN_CONTAINER = "/etc/pipelock-ca-key.pem"


def pipelock_container_name(slug: str) -> str:
    return f"claude-bottle-pipelock-{slug}"


def pipelock_proxy_url(slug: str) -> str:
    return f"http://{pipelock_container_name(slug)}:{PIPELOCK_PORT}"


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


class DockerPipelockProxy(PipelockProxy):
    """Docker-flavored PipelockProxy: inherits `.prepare()` from the
    base, exposes the in-container CA paths the renderer reads.
    Container lifecycle is owned by compose."""

    CA_CERT_IN_CONTAINER = PIPELOCK_CA_CERT_IN_CONTAINER
    CA_KEY_IN_CONTAINER = PIPELOCK_CA_KEY_IN_CONTAINER

