"""The CLI's host state, asked of the host controller: the host DB's only
writer (ADR 0020), so the CLI never opens the file. Each call starts the host
controller if it isn't running, as a launch does.

A refused request raises `HostControlRefused` with the host controller's
message in `error`; an unreachable one, `HostControlError`.

Forge connections and trust records are reached through their own modules
(`forge_connections`, `manifest.trust`), which code shared with the host
controller calls: outside it, they ask it the same way (`host_access`).
"""

from __future__ import annotations

import dataclasses
from typing import Any

from .. import host_access
from ..contract.hostctl import (
    AUDIT_IMPORT_PATH, AUDIT_PASSKEYS_PATH, AUDIT_QUERY_PATH, AUDIT_REBUILD_PATH,
    HOST_CONFIG_PATH, PAIRING_PATH,
)
from ..control.hostctl import runner
from ..control.hostctl.audit_index import Query
from ..control.hostctl.audit_trust import Trust
from ..control.hostctl.client import HostControlClient
from ..control.hostctl.pairing import Pairing
from ..lib.config_store import HostConfig

# The audit routes index the whole journal on first use, which can take a while.
_AUDIT_TIMEOUT_SECONDS = 600.0


def _client() -> HostControlClient:
    return host_access.client()


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


def _pairing_reply(reply: dict[str, object]) -> tuple[Pairing, dict[str, Any]]:
    link = reply.get("link")
    return Pairing.from_json(reply.get("pairing")), dict(link) if isinstance(link, dict) else {}


def pairing() -> tuple[Pairing, dict[str, Any]]:
    """This host's pairing with a console, and the link's last state ({}
    before it has one)."""
    return _pairing_reply(_client().get(PAIRING_PATH))


def running_pairing() -> tuple[Pairing, dict[str, Any]] | None:
    """`pairing()`, or None when no host controller is running: one isn't
    started for it. The link only runs in the host controller."""
    state = runner.running()
    if state is None:
        return None
    return _pairing_reply(HostControlClient(state, runner.cli_token()).get(PAIRING_PATH))


def set_pairing(*, new: bool = False, **changes: object) -> Pairing:
    """Change the pairing (`PAIRING_PATH`): a `new` one clears what
    `changes` doesn't name. `access_token` and `refresh_token` are values,
    kept in the host secrets file."""
    reply = _client().post(PAIRING_PATH, {"changes": dict(changes), "new": new})
    return _pairing_reply(reply)[0]


def audit_passkeys() -> Trust:
    """The audit passkeys this host trusts."""
    return Trust.from_json(_client().get(AUDIT_PASSKEYS_PATH))


def trust_passkeys(trust: Trust) -> Trust:
    """Trust exactly `trust`'s passkeys; what's trusted now."""
    return Trust.from_json(_client().post(AUDIT_PASSKEYS_PATH, trust.to_json()))


__all__ = ["audit_import", "audit_passkeys", "audit_query", "audit_rebuild", "host_config",
           "pairing", "running_pairing", "set_host_config", "set_pairing", "trust_passkeys"]
