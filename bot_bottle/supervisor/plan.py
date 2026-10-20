"""`SupervisePlan` — the launch-time supervise staging DTO.

A pure value type (no store access): the backend builds it at launch in
`bot_bottle.backend.resolve_common.prepare_supervise`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SupervisePlan:
    """Output of `prepare_supervise`; consumed by `.start`.

    `db_path` is the host database bind-mounted into the gateway at
    /run/supervise/bot-bottle.db. `internal_network` is empty at prepare time;
    the backend's launch step fills it via `dataclasses.replace` before calling
    `.start`."""

    slug: str
    db_path: Path
    internal_network: str = ""
