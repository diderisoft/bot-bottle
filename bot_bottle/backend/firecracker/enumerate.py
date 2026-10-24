"""Firecracker active-agent listing: firecracker processes whose
`--config-file` is in an existing run dir. Scan failures raise
`EnumerationError` rather than read as empty.
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
            # The gateway is shared; there are no per-bottle services to
            # report.
            services=(),
            label=metadata.label if metadata else "",
            color=metadata.color if metadata else "",
        ))
    return out
