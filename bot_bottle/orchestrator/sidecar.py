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

from .dockerutil import run_docker

SIDECAR_NAME = "bot-bottle-orch-sidecar"
SIDECAR_LABEL = "bot-bottle-orch-sidecar=1"


class SidecarError(Exception):
    """The shared sidecar failed to start/stop (non-zero `docker` exit)."""


class Sidecar(abc.ABC):
    """Lifecycle of the single per-host sidecar. Backend-neutral."""

    name: str

    @abc.abstractmethod
    def ensure_running(self) -> None:
        """Start the sidecar if it isn't already up. Idempotent: a no-op
        when it's already running (that's the whole point — one per host)."""

    @abc.abstractmethod
    def is_running(self) -> bool:
        """True iff the sidecar instance is currently up."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Remove the sidecar. Idempotent — absent is success."""


class DockerSidecar(Sidecar):
    """The consolidated sidecar as a single, fixed-name Docker container."""

    def __init__(self, image_ref: str, *, name: str = SIDECAR_NAME) -> None:
        self.image_ref = image_ref
        self.name = name

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


__all__ = ["Sidecar", "DockerSidecar", "SidecarError", "SIDECAR_NAME", "SIDECAR_LABEL"]
