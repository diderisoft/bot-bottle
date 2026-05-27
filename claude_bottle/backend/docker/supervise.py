"""DockerSupervise — Docker-flavored supervise config (PRD 0013).
Inherits the platform-agnostic prepare step (queue dir +
current-config staging) from `Supervise`. The supervise daemon
runs inside the sidecar bundle (PRD 0024); this module just holds
the container-name helper the renderer's network alias targets."""

from __future__ import annotations

from ...supervise import Supervise


def supervise_container_name(slug: str) -> str:
    """The legacy per-sidecar container name. Kept as a function so
    the renderer can register it as a docker-network alias on the
    bundle — any code still referring to
    `claude-bottle-supervise-<slug>` resolves to the bundle's IP."""
    return f"claude-bottle-supervise-{slug}"


class DockerSupervise(Supervise):
    """Docker-flavored Supervise: inherits `.prepare()` from the base.
    The supervise daemon's container lifecycle is owned by the
    sidecar bundle (PRD 0024)."""
