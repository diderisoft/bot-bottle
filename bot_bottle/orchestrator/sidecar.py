"""The consolidated per-host sidecar (PRD 0070).

The core consolidation win: **one** persistent sidecar per host, shared by
every bottle, instead of a sidecar bundle per bottle. It's safe to share
because the attribution invariant (source IP + identity token, see
`registry`) lets the sidecar attribute each request to the right bottle —
so per-bottle policy lives in one long-lived process keyed on who's calling.

`Sidecar` is the backend-neutral lifecycle contract (mirrors `LaunchBroker`):
ensure the single instance is up, report it, tear it down. `DockerSidecar`
is the docker implementation; a firecracker sidecar VM slots in later.

The defining behaviour is **idempotent singleton**: `ensure_running` starts
the instance if absent and is a no-op if it's already up, so N bottle
launches never spawn N sidecars.
"""

from __future__ import annotations

import abc
import os
from pathlib import Path

from ..docker_cmd import run_docker

SIDECAR_NAME = "bot-bottle-orch-sidecar"
SIDECAR_LABEL = "bot-bottle-orch-sidecar=1"

# The real sidecar-bundle image + its Dockerfile. Kept as a local constant
# rather than imported from backend.docker.sidecar_bundle, which would drag
# the whole backend layer into the lean orchestrator (see #359); unify when
# that lands. Env override matches the backend's BOT_BOTTLE_SIDECAR_IMAGE.
SIDECAR_BUNDLE_IMAGE = os.environ.get("BOT_BOTTLE_SIDECAR_IMAGE", "bot-bottle-sidecars:latest")
SIDECAR_DOCKERFILE = "Dockerfile.sidecars"
_REPO_ROOT = Path(__file__).resolve().parents[2]


class SidecarError(Exception):
    """The shared sidecar failed to build/start/stop (non-zero `docker` exit)."""


class Sidecar(abc.ABC):
    """Lifecycle of the single per-host sidecar. Backend-neutral."""

    name: str

    def ensure_built(self) -> None:
        """Ensure the sidecar's image / rootfs exists, building it if needed.
        Default: nothing to build (e.g. a stub or a pre-pulled image)."""
        return

    @abc.abstractmethod
    def ensure_running(self) -> None:
        """Start the sidecar if it isn't already up. Idempotent: a no-op
        when it's already running (that's the whole point — one per host).
        Assumes the image exists — call `ensure_built()` first."""

    @abc.abstractmethod
    def is_running(self) -> bool:
        """True iff the sidecar instance is currently up."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Remove the sidecar. Idempotent — absent is success."""


class DockerSidecar(Sidecar):
    """The consolidated sidecar as a single, fixed-name Docker container.

    `image_ref` defaults to the real sidecar-bundle image; `ensure_built`
    builds it from `Dockerfile.sidecars` when it's missing. (Note: slice 5
    builds + launches the bundle container; wiring its per-bottle,
    source-IP-keyed config is a later slice — see PRD 0070.)"""

    def __init__(
        self,
        image_ref: str = SIDECAR_BUNDLE_IMAGE,
        *,
        name: str = SIDECAR_NAME,
        build_context: Path | None = None,
        dockerfile: str | None = SIDECAR_DOCKERFILE,
    ) -> None:
        self.image_ref = image_ref
        self.name = name
        self._build_context = build_context or _REPO_ROOT
        self._dockerfile = dockerfile

    def image_exists(self) -> bool:
        return run_docker(["docker", "image", "inspect", self.image_ref]).returncode == 0

    def ensure_built(self) -> None:
        """Build the bundle image from its Dockerfile when it's missing.
        No-op when the image is already present, or when no dockerfile is
        configured (e.g. a pre-pulled image)."""
        if self._dockerfile is None or self.image_exists():
            return
        proc = run_docker([
            "docker", "build",
            "-t", self.image_ref,
            "-f", str(self._build_context / self._dockerfile),
            str(self._build_context),
        ])
        if proc.returncode != 0:
            raise SidecarError(f"sidecar image build failed: {proc.stderr.strip()}")

    def is_running(self) -> bool:
        proc = run_docker([
            "docker", "ps",
            "--filter", f"name=^{self.name}$",
            "--filter", "status=running",
            "--format", "{{.Names}}",
        ])
        return self.name in proc.stdout.split()

    def ensure_running(self) -> None:
        if self.is_running():
            return
        # Clear any stale (stopped) container holding the fixed name, then
        # start fresh. `rm --force` on an absent name is a tolerated no-op.
        run_docker(["docker", "rm", "--force", self.name])
        proc = run_docker([
            "docker", "run", "--detach",
            "--name", self.name,
            "--label", SIDECAR_LABEL,
            self.image_ref,
        ])
        if proc.returncode != 0:
            raise SidecarError(f"sidecar failed to start: {proc.stderr.strip()}")

    def stop(self) -> None:
        proc = run_docker(["docker", "rm", "--force", self.name])
        if proc.returncode != 0 and "No such container" not in proc.stderr:
            raise SidecarError(f"sidecar failed to stop: {proc.stderr.strip()}")


__all__ = [
    "Sidecar", "DockerSidecar", "SidecarError",
    "SIDECAR_NAME", "SIDECAR_LABEL", "SIDECAR_BUNDLE_IMAGE",
]
