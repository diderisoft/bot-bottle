"""Docker-side supervise helpers: container naming for the legacy
per-sidecar topology (kept so the bundle's docker-network alias
resolves the old name to the bundle IP). The prepare-time
queue-dir + current-config staging lives on the platform-neutral
`Supervise` ABC — backends instantiate it directly. The
supervise daemon's container lifecycle is owned by the sidecar
bundle (PRD 0024)."""

from __future__ import annotations


def supervise_container_name(slug: str) -> str:
    """The legacy per-sidecar container name. Kept as a function so
    the renderer can register it as a docker-network alias on the
    bundle — any code still referring to
    `claude-bottle-supervise-<slug>` resolves to the bundle's IP."""
    return f"claude-bottle-supervise-{slug}"
