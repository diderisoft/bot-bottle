"""Active-agent enumeration for the Firecracker backend.

Running bottles are the Firecracker processes whose ``--config-file`` points
at an existing per-bottle run directory. The same authoritative process scan
protects cleanup from deleting live VMs; operational scan failures propagate
as ``EnumerationError`` instead of masquerading as an empty host.
"""

from __future__ import annotations

from ...bottle_state import read_metadata
from .. import ActiveAgent
from .cleanup import live_run_dirs


def enumerate_active() -> list[ActiveAgent]:
    out: list[ActiveAgent] = []
    for run_dir in live_run_dirs():
        slug = run_dir.name
        metadata = read_metadata(slug)
        out.append(ActiveAgent(
            backend_name="firecracker",
            slug=slug,
            agent_name=metadata.agent_name if metadata else "?",
            started_at=metadata.started_at if metadata else "",
            # Firecracker uses the shared gateway, so there are no
            # per-bottle gateway service containers to report.
            services=(),
            label=metadata.label if metadata else "",
            color=metadata.color if metadata else "",
        ))
    return out
