"""Active-agent enumeration for the macOS Apple Container backend."""

from __future__ import annotations

import subprocess

from ...bottle_state import read_metadata
from .. import ActiveAgent, EnumerationError
from . import util as container_mod
from .infra import INFRA_NAME, ORCHESTRATOR_NAME

# Every agent container is `bot-bottle-<slug>`.
CONTAINER_NAME_PREFIX = "bot-bottle-"
# The infra containers share the prefix but aren't bottles.
_INFRA_NAMES = frozenset({INFRA_NAME, ORCHESTRATOR_NAME})


def enumerate_active() -> list[ActiveAgent]:
    try:
        result = subprocess.run(
            ["container", "list", "--quiet"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise EnumerationError(
            "container list failed: container CLI not found"
        ) from exc
    if result.returncode != 0:
        if not container_mod.service_running():
            return []  # nothing runs while the service is down
        raise EnumerationError(
            f"container list failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}"
        )
    out: list[ActiveAgent] = []
    for name in sorted(line.strip() for line in result.stdout.splitlines()):
        if not name.startswith(CONTAINER_NAME_PREFIX) or name in _INFRA_NAMES:
            continue
        slug = name[len(CONTAINER_NAME_PREFIX):]
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
