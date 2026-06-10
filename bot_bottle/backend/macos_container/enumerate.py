"""Active-agent enumeration for the macOS Apple Container backend."""

from __future__ import annotations

import subprocess

from ...bottle_state import read_metadata
from .. import ActiveAgent

_PREFIX = "bot-bottle-"
_SIDECAR_PREFIX = "bot-bottle-sidecars-"


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
        if not name.startswith(_PREFIX):
            continue
        if name.startswith(_SIDECAR_PREFIX):
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
