"""Active-agent enumeration for the Firecracker backend.

The agent runs in a VM (no container to list), so a live bottle is
identified by its running sidecar container `bot-bottle-sidecars-<slug>`
— the same discovery-by-prefix the other backends use.
"""

from __future__ import annotations

import subprocess

from ...bottle_state import read_metadata
from .. import ActiveAgent

_SIDECAR_PREFIX = "bot-bottle-sidecars-"


def enumerate_active() -> list[ActiveAgent]:
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}",
         "--filter", f"name={_SIDECAR_PREFIX}"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return []
    out: list[ActiveAgent] = []
    for name in sorted(n.strip() for n in result.stdout.splitlines() if n.strip()):
        slug = name[len(_SIDECAR_PREFIX):]
        metadata = read_metadata(slug)
        if metadata is None or metadata.backend != "firecracker":
            # Skip sidecars owned by another backend (docker/smolmachines
            # share the container-name prefix).
            continue
        out.append(ActiveAgent(
            backend_name="firecracker",
            slug=slug,
            agent_name=metadata.agent_name,
            started_at=metadata.started_at,
            services=(),
            label=metadata.label,
            color=metadata.color,
        ))
    return out
