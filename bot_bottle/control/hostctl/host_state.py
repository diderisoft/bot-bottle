"""The host DB's routes for the CLI (`HOST_CONFIG_PATH`, the audit index's,
`FORGES_PATH`, `FORGES_REMOVE_PATH`, `TRUST_PATH`, `PAIRING_PATH`,
`AUDIT_PASSKEYS_PATH`). The host controller is the host DB's only
writer, so the CLI reads and changes host state here, never by opening the
file (ADR 0020). These use the stores, never the routed functions shared code
calls (`forge_connections.connections()`, `trust.load_trusted()`), which would
ask here again.

Each function takes the request body and returns the reply. ValueError means
a bad request; `IndexBreak` and `AuditImportError`, a journal that can't be
indexed or a segment that can't be imported.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from ... import forge_connections
from ...forge_connections import ForgeConnection, ForgeConnectionError, ForgeStore
from ...errors import SecretsFileError
from ...lib.config_store import ConfigStore
from ...manifest.trust import Trusted, TrustStore
from . import audit_trust, pairing
from ...paths import host_audit_dir, host_db_path
from .audit_import import import_segment
from .audit_index import AuditIndex, Query

_QUERY_FIELDS = {f.name for f in dataclasses.fields(Query)}


def config() -> dict[str, object]:
    """Every host setting."""
    return ConfigStore(host_db_path()).host_config().to_json()


def set_config(body: dict[str, object]) -> dict[str, object]:
    """Set the settings `body` names, all or none, and answer with every
    setting."""
    store = ConfigStore(host_db_path())
    changed = store.host_config()
    for name, value in body.items():
        changed = changed.with_json(name, value)
    store.set_host_config(changed)
    return store.host_config().to_json()


def _index() -> AuditIndex:
    return AuditIndex(host_audit_dir(), host_db_path())


def audit_query(body: dict[str, object]) -> dict[str, object]:
    """The indexed records `body`'s filters select, after indexing new ones."""
    unknown = set(body) - _QUERY_FIELDS
    if unknown:
        raise ValueError(f"unknown filter(s): {', '.join(sorted(unknown))}")
    for name, value in body.items():
        if name == "limit":
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                raise ValueError("limit must be a whole number")
        elif value is not None and not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
    index = _index()
    index.sync()
    return {"records": index.query(Query(**body))}  # type: ignore[arg-type]


def audit_rebuild(body: dict[str, object]) -> dict[str, object]:
    """Re-create the index from the journal and imported chains."""
    del body
    return {"indexed": _index().rebuild()}


def audit_import(body: dict[str, object]) -> dict[str, object]:
    """Import the segment file `body["segment"]` names (an absolute path on
    this host) as its own chain."""
    segment = body.get("segment")
    if not isinstance(segment, str) or not Path(segment).is_absolute():
        raise ValueError("segment must be an absolute path")
    directory = host_audit_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = import_segment(directory, Path(segment), _index())
    return {"chain": str(result.chain.relative_to(directory)), "records": result.records,
            "extended": result.extended, "already_imported": result.already_imported}


def forges(body: dict[str, object]) -> dict[str, object]:
    """Every forge this host connected."""
    del body
    return {"forges": [c.to_json() for c in ForgeStore(host_db_path()).connections().values()]}


def forge_connect(body: dict[str, object]) -> dict[str, object]:
    """Record `body["user"]` connected on `body["forge"]`."""
    make_default = body.get("make_default", False)
    if not isinstance(make_default, bool):
        raise ValueError("make_default must be true or false")
    raw = body.get("forge")
    try:
        conn = ForgeConnection.from_json({**raw, "users": [body.get("user")]}
                                         if isinstance(raw, dict) else raw)
        stored = ForgeStore(host_db_path()).add_user(
            conn, conn.users[0], make_default=make_default)
    except ForgeConnectionError as e:
        raise ValueError(str(e)) from e
    return {"forge": stored.to_json()}


def forge_remove(body: dict[str, object]) -> dict[str, object]:
    """Disconnect `body["user"]`, or every user, from `body["host"]`."""
    host, user = body.get("host"), body.get("user", "")
    if not isinstance(host, str) or not host or not isinstance(user, str):
        raise ValueError("host must name a forge, and user a user on it")
    try:
        return {"removed": forge_connections.forget(host, user)}
    except ForgeConnectionError as e:
        raise ValueError(str(e)) from e


def trusted(body: dict[str, object]) -> dict[str, object]:
    """What this host trusts."""
    del body
    return {"trusted": [t.to_json() for t in TrustStore(host_db_path()).records()]}


def trust(body: dict[str, object]) -> dict[str, object]:
    """Trust the record `body` is, replacing the one for its path."""
    TrustStore(host_db_path()).put(Trusted.from_json(body))
    return trusted({})


def _pairing_reply(store: pairing.PairingStore) -> dict[str, object]:
    return {"pairing": store.pairing().to_json(), "link": store.status()}


def get_pairing(body: dict[str, object]) -> dict[str, object]:
    """This host's pairing with a console, and the link's last state."""
    del body
    return _pairing_reply(pairing.store())


def set_pairing(body: dict[str, object]) -> dict[str, object]:
    """Change the pairing: `body["changes"]`, from nothing if `body["new"]`.
    `pair`, `login` and their token refreshes all write it here."""
    changes, new = body.get("changes"), body.get("new", False)
    if not isinstance(changes, dict) or not isinstance(new, bool):
        raise ValueError("expected {changes: {...}, new: true|false}")
    store = pairing.store()
    try:
        store.update({str(k): v for k, v in changes.items()}, new=new)
    except SecretsFileError as e:
        raise ValueError(str(e)) from e
    return _pairing_reply(store)


def passkeys(body: dict[str, object]) -> dict[str, object]:
    """The audit passkeys this host trusts."""
    del body
    return audit_trust.load(host_db_path()).to_json()


def trust_passkeys(body: dict[str, object]) -> dict[str, object]:
    """Trust exactly the passkeys `body` lists, for its origin."""
    try:
        wanted = audit_trust.Trust.from_json(body)
    except audit_trust.TrustError as e:
        raise ValueError(str(e)) from e
    return audit_trust.PasskeyStore(host_db_path()).replace(wanted).to_json()


__all__ = ["audit_import", "audit_query", "audit_rebuild", "config", "forge_connect",
           "forge_remove", "forges", "get_pairing", "passkeys", "set_config", "set_pairing",
           "trust", "trust_passkeys", "trusted"]
