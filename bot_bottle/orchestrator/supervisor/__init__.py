"""The orchestrator's view of the supervise plane.

Bundles the orchestrator-owned supervise surface: the queue/audit I/O and diff
rendering (`queue`) and the `Supervise` lifecycle (`supervise`). For
convenience it re-exports the neutral vocabulary (`bot_bottle.supervisor`'s
types + `SupervisePlan`) so orchestrator-side callers — service, CLI, tests —
import from one place.

The data plane must NOT import this package; it imports `bot_bottle.supervisor`
(neutral) directly and reaches the queue over the control-plane RPC.
"""

from __future__ import annotations

from ...supervisor.types import *  # noqa: F401,F403 — re-export the neutral vocabulary
from ...supervisor.plan import SupervisePlan
from .queue import (
    archive_all_proposals,
    archive_proposal,
    audit_dir,
    audit_log_path,
    list_all_pending_proposals,
    list_pending_proposals,
    read_audit_entries,
    read_proposal,
    read_response,
    render_diff,
    sha256_hex,
    wait_for_response,
    write_audit_entry,
    write_proposal,
    write_response,
)
from .supervise import Supervise
