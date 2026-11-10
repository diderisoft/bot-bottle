"""The host DB's routes for the CLI (`HOST_CONFIG_PATH`, `AUDIT_QUERY_PATH`,
`AUDIT_REBUILD_PATH`, `AUDIT_IMPORT_PATH`). The host controller is the host
DB's only writer, so the CLI reads and changes host state here, never by
opening the file (ADR 0020).

Each function takes the request body and returns the reply. ValueError means
a bad request; `IndexBreak` and `AuditImportError`, a journal that can't be
indexed or a segment that can't be imported.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from ...lib.config_store import ConfigStore
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


__all__ = ["audit_import", "audit_query", "audit_rebuild", "config", "set_config"]
