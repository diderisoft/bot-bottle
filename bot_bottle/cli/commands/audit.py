"""audit: check and search the host's audit journal."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ...contract.audit import canonical
from ...control.hostctl.audit_import import AuditImportError, import_segment
from ...control.hostctl.audit_index import (
    IMPORTED_DIRNAME,
    AuditIndex,
    IndexBreak,
    Query,
    imported_chains,
)
from ...control.hostctl.audit_ocsf import to_ocsf
from ...control.hostctl.audit_verify import verify
from ...lib.config_store import AuditForward, ConfigStore
from ...manifest.bottle import AUDIT_DELIVERIES
from ...paths import host_audit_dir, host_db_path
from ..constants import PROG


def _verify(_ns: argparse.Namespace) -> int:
    directory = host_audit_dir()
    if not directory.is_dir():
        sys.stdout.write(f"no audit journal at {directory}\n")
        return 0
    ok = True
    for chain in [directory, *imported_chains(directory)]:
        label = ("local" if chain == directory
                 else f"imported {chain.relative_to(directory / IMPORTED_DIRNAME)}")
        report = verify(chain)
        ok = ok and report.ok
        for problem in report.problems:
            sys.stdout.write(f"{problem}\n")
        if report.starts_after:
            sys.stdout.write(f"{label}: starts mid-chain (older segments were removed)\n")
        verdict = "ok" if report.ok else f"BROKEN ({len(report.breaks)} break(s))"
        sys.stdout.write(f"{label}: {verdict}: {report.records} record(s) in "
                         f"{report.segments} segment(s), head {report.head[:16] or '-'}\n")
    return 0 if ok else 1


def _index() -> AuditIndex | None:
    directory = host_audit_dir()
    if not directory.is_dir():
        sys.stdout.write(f"no audit journal at {directory}\n")
        return None
    return AuditIndex(directory, host_db_path())


_COLUMNS = ("ts_event", "type", "outcome", "bottle", "actor", "resource")


def _table(records: list[dict[str, Any]]) -> None:
    rows = [[str(r.get(c) if r.get(c) is not None else "-") for c in _COLUMNS]
            for r in records]
    widths = [max([len(c)] + [len(row[i]) for row in rows]) for i, c in enumerate(_COLUMNS)]
    for row in [list(_COLUMNS), *rows]:
        sys.stdout.write("  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip() + "\n")


def _matching(ns: argparse.Namespace) -> list[dict[str, Any]] | None:
    """The indexed records `ns`'s filters select, after syncing the index;
    None (already reported) when there's no journal or it doesn't chain."""
    index = _index()
    if index is None:
        return None
    try:
        index.sync()
    except IndexBreak as e:
        sys.stderr.write(f"{PROG}: {e}; run `{PROG} audit verify`\n")
        return None
    return index.query(Query(
        since=ns.since, until=ns.until, type=ns.type, host=ns.host,
        bottle=ns.bottle, activation=ns.activation, bottled_agent=ns.agent,
        actor=ns.actor, outcome=ns.outcome, repository=ns.repository,
        correlation_id=ns.correlation_id, commit_sha=ns.commit, limit=ns.limit,
    ))


def _query(ns: argparse.Namespace) -> int:
    records = _matching(ns)
    if records is None:
        return 0 if not host_audit_dir().is_dir() else 1
    if ns.json:
        for record in records:
            sys.stdout.write(canonical(record) + "\n")
    else:
        _table(records)
    return 0


def _export(ns: argparse.Namespace) -> int:
    """JSONL on stdout: the journal's own lines, or their OCSF 1.9.0 events."""
    records = _matching(ns)
    if records is None:
        return 0 if not host_audit_dir().is_dir() else 1
    for record in records:
        out = to_ocsf(record) if ns.format == "ocsf" else record
        sys.stdout.write(canonical(out) + "\n")
    return 0


def _rebuild(_ns: argparse.Namespace) -> int:
    index = _index()
    if index is None:
        return 0
    try:
        count = index.rebuild()
    except IndexBreak as e:
        sys.stderr.write(f"{PROG}: {e}\n")
        return 1
    sys.stdout.write(f"indexed {count} record(s)\n")
    return 0


def _import(ns: argparse.Namespace) -> int:
    directory = host_audit_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    index = AuditIndex(directory, host_db_path())
    try:
        result = import_segment(directory, Path(ns.segment), index)
    except (AuditImportError, IndexBreak, OSError) as e:
        sys.stderr.write(f"{PROG}: can't import {ns.segment}: {e}\n")
        return 1
    where = result.chain.relative_to(directory)
    if result.already_imported:
        sys.stdout.write(f"already imported into {where}\n")
    else:
        how = "extended" if result.extended else "new chain"
        sys.stdout.write(f"imported {result.records} record(s) into {where} ({how})\n")
    return 0


def _delivery(ns: argparse.Namespace) -> int:
    store = ConfigStore(host_db_path())
    if ns.mode:
        store.migrate()
        store.set_audit_delivery(ns.mode)
    sys.stdout.write(f"{store.audit_delivery()}\n")
    return 0


def _forward(ns: argparse.Namespace) -> int:
    store = ConfigStore(host_db_path())
    if ns.off or ns.url:
        store.migrate()
        store.set_audit_forward(
            None if ns.off else AuditForward(url=ns.url, secret=ns.secret or "",
                                             format=ns.format))
    forward = store.audit_forward()
    if forward is None:
        sys.stdout.write("not forwarding\n")
    else:
        token = f", bearer token from secret {forward.secret}" if forward.secret else ""
        sys.stdout.write(f"forwarding {forward.format} to {forward.url}{token}\n")
    return 0


def cmd_audit(args: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{PROG} audit",
        description="Check and search the host's audit journal.",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser(
        "verify",
        help="check the journal's hash chain; exits non-zero on any break",
    ).set_defaults(run=_verify)
    query = sub.add_parser("query", help="list matching events, oldest first")
    export = sub.add_parser("export", help="matching events as JSONL, for a SIEM or a file")
    export.add_argument("--format", choices=("native", "ocsf"), default="native",
                        help="native: the journal's own lines; ocsf: OCSF 1.9.0 events")
    for flag, dest, text in (
        ("--since", "since", "events at or after this ISO time"),
        ("--until", "until", "events before this ISO time"),
        ("--type", "type", "event type, or a group glob like egress.*"),
        ("--host", "host", None), ("--bottle", "bottle", None),
        ("--activation", "activation", None),
        ("--agent", "agent", "bottled-agent slug"),
        ("--actor", "actor", None), ("--outcome", "outcome", None),
        ("--repository", "repository", None),
        ("--correlation-id", "correlation_id", None),
        ("--commit", "commit", "commit SHA"),
    ):
        query.add_argument(flag, dest=dest, help=text)
        export.add_argument(flag, dest=dest, help=text)
    query.add_argument("--limit", type=int, default=None)
    export.add_argument("--limit", type=int, default=None)
    export.set_defaults(run=_export)
    query.add_argument("--json", action="store_true",
                       help="each record exactly as the journal holds it")
    query.set_defaults(run=_query)
    sub.add_parser(
        "rebuild", help="re-create the search index from the journal",
    ).set_defaults(run=_rebuild)
    imp = sub.add_parser(
        "import", help="add a segment from another host or a backup, kept as its own chain")
    imp.add_argument("segment", help="a journal segment file (.jsonl)")
    imp.set_defaults(run=_import)
    delivery = sub.add_parser(
        "delivery",
        help="show or set the default audit.delivery for bottles that don't set it")
    delivery.add_argument("mode", nargs="?", choices=AUDIT_DELIVERIES)
    delivery.set_defaults(run=_delivery)
    forward = sub.add_parser(
        "forward", help="show or set where the host controller forwards the journal")
    forward.add_argument("--url", help="an https endpoint that accepts NDJSON POSTs")
    forward.add_argument("--secret", help="name of the secrets-file entry sent as a bearer token")
    forward.add_argument("--format", choices=("native", "ocsf"), default="native")
    forward.add_argument("--off", action="store_true", help="stop forwarding")
    forward.set_defaults(run=_forward)
    ns = parser.parse_args(args)
    return ns.run(ns)
