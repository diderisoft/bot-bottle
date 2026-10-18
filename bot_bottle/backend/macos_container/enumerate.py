"""Active-agent enumeration for the macOS Apple Container backend."""

from __future__ import annotations

import subprocess

from ...bottle_state import read_metadata
from .. import ActiveAgent, EnumerationError
from . import util as container_mod
from .infra import INFRA_NAME, ORCHESTRATOR_NAME

# The name every agent container carries: `bot-bottle-<slug>`. Exported
# because callers that act on a running bottle (gateway-host rewrites,
# registry reconciliation) have to map an enumerated slug back to a
# container name.
CONTAINER_NAME_PREFIX = "bot-bottle-"
# The two shared per-host infra containers (orchestrator + gateway) carry the
# same `bot-bottle-` prefix as agent containers but are infrastructure, not
# bottles — one pair serves every agent, so enumerating either as an agent would
# invent a phantom bottle per host.
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
            return []  # no container runs while the service is down
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
