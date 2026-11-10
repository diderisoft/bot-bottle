"""The CLI's host state, asked of the host controller: the host DB's only
writer (ADR 0020), so the CLI never opens the file. Each call starts the host
controller if it isn't running, as a launch does.

A refused request raises `HostControlRefused` with the host controller's
message in `error`; an unreachable one, `HostControlError`.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from ..contract.hostctl import (
    AUDIT_IMPORT_PATH, AUDIT_QUERY_PATH, AUDIT_REBUILD_PATH, HOST_CONFIG_PATH,
)
from ..control.hostctl import runner
from ..control.hostctl.audit_index import Query
from ..control.hostctl.client import HostControlClient
from ..lib.config_store import HostConfig

# The audit routes index the whole journal on first use, which can take a while.
_AUDIT_TIMEOUT_SECONDS = 600.0


def _client() -> HostControlClient:
    return HostControlClient(runner.ensure_running(), runner.cli_token())


def host_config() -> HostConfig:
    """Every host setting."""
    return HostConfig.from_json(_client().get(HOST_CONFIG_PATH))


def set_host_config(config: HostConfig, *names: str) -> HostConfig:
    """Store `config`'s settings `names` (all of them when none are named);
    the settings as they now are."""
    body = config.to_json()
    if names:
        body = {name: body[name] for name in names}
    return HostConfig.from_json(_client().post(HOST_CONFIG_PATH, body))


def audit_query(query: Query) -> list[dict[str, Any]]:
    """The indexed records `query` selects, after the index catches up."""
    reply = _client().post(AUDIT_QUERY_PATH, dataclasses.asdict(query),
                           timeout=_AUDIT_TIMEOUT_SECONDS)
    return list(reply["records"])  # type: ignore[arg-type]


def audit_rebuild() -> int:
    """Re-create the index; how many records it holds."""
    reply = _client().post(AUDIT_REBUILD_PATH, {}, timeout=_AUDIT_TIMEOUT_SECONDS)
    return int(str(reply["indexed"]))


def audit_import(segment: str) -> dict[str, object]:
    """Import the segment at absolute path `segment`: {"chain", "records",
    "extended", "already_imported"}."""
    return _client().post(AUDIT_IMPORT_PATH, {"segment": segment},
                          timeout=_AUDIT_TIMEOUT_SECONDS)


__all__ = ["audit_import", "audit_query", "audit_rebuild", "host_config", "set_host_config"]
