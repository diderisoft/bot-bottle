"""Active-agent enumeration for the macOS Apple Container backend."""

from __future__ import annotations

import subprocess

from ...bottle_state import read_metadata
from .. import ActiveAgent
from .infra import INFRA_NAME

_PREFIX = "bot-bottle-"
# The shared per-host infra container carries the same prefix as agent
# containers but is infrastructure, not a bottle — one control plane + gateway
# serves every agent, so listing it as an agent would invent one per host.
_INFRA_NAMES = frozenset({INFRA_NAME})


def enumerate_active() -> list[ActiveAgent]:
    result = subprocess.run(
        ["container", "list", "--quiet"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    out: list[ActiveAgent] = []
    for name in sorted(line.strip() for line in result.stdout.splitlines()):
        if not name.startswith(_PREFIX) or name in _INFRA_NAMES:
            continue
        slug = name[len(_PREFIX):]
        metadata = read_metadata(slug)
        out.append(ActiveAgent(
            backend_name="macos-container",
            slug=slug,
            agent_name=metadata.agent_name if metadata else "?",
            started_at=metadata.started_at if metadata else "",
            services=(),
            label=metadata.label if metadata else "",
            color=metadata.color if metadata else "",
        ))
    return out
