"""`SupervisePlan` — the launch-time supervise staging DTO.

A pure value type (no store access), so it lives in the neutral package: the
backend builds it at launch and the orchestrator's `Supervise.prepare` returns
it. The behaviour that fills it in — staging the host database — is the
orchestrator's, in `bot_bottle.orchestrator.supervisor.supervise`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SupervisePlan:
    """Output of `Supervise.prepare`; consumed by `.start`.

    `db_path` is the host database bind-mounted into the gateway at
    /run/supervise/bot-bottle.db. `internal_network` is empty at prepare time;
    the backend's launch step fills it via `dataclasses.replace` before calling
    `.start`."""

    slug: str
    db_path: Path
    internal_network: str = ""
