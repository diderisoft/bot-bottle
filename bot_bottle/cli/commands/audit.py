"""audit: check and search the host's audit journal.

The index and the settings are in the host DB, so those subcommands go
through the host controller (`host_state_client`), starting it if needed.
`verify` only reads the journal's files.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shlex
import sys
from pathlib import Path
from typing import Any

from ...contract.audit import canonical
from ...control.hostctl.audit_index import IMPORTED_DIRNAME, Query, imported_chains
from ...control.hostctl.audit_ocsf import to_ocsf
from ...control.hostctl.audit_verify import verify
from ...control.hostctl.client import HostControlRefused
from ...control.hostctl.runner import HostControlError
from ...lib.config_store import (
    AUDIT_DELIVERIES, AuditForward, AuditRetention, FirehoseSink, HostConfig,
)
from ...paths import host_audit_dir
from .. import host_state_client as host
from ..constants import PROG
from . import audit_passkeys


def _verify(_ns: argparse.Namespace) -> int:
    directory = host_audit_dir()
    if not directory.is_dir():
        sys.stdout.write(f"no audit journal at {directory}\n")
        return 0
    ok = True
    for chain in [directory, *imported_chains(directory)]:
        label = ("local" if chain == directory
                 else f"imported {chain.relative_to(directory / IMPORTED_DIRNAME)}")
        report = verify(chain, anchored=chain == directory)
        ok = ok and report.ok
        for problem in report.problems:
            sys.stdout.write(f"{problem}\n")
        if report.pruned_at:
            sys.stdout.write(f"{label}: older segments were pruned; the chain starts from "
                             f"the journal.pruned record at {report.pruned_at}\n")
        elif report.starts_after:
            sys.stdout.write(f"{label}: starts mid-chain (older segments were removed)\n")
        verdict = "ok" if report.ok else f"BROKEN ({len(report.breaks)} break(s))"
        sys.stdout.write(f"{label}: {verdict}: {report.records} record(s) in "
                         f"{report.segments} segment(s), head {report.head[:16] or '-'}\n")
    return 0 if ok else 1


def _have_journal() -> bool:
    directory = host_audit_dir()
    if not directory.is_dir():
        sys.stdout.write(f"no audit journal at {directory}\n")
        return False
    return True


_COLUMNS = ("ts_event", "type", "outcome", "bottle", "actor", "resource")


def _table(records: list[dict[str, Any]]) -> None:
    rows = [[str(r.get(c) if r.get(c) is not None else "-") for c in _COLUMNS]
            for r in records]
    widths = [max([len(c)] + [len(row[i]) for row in rows]) for i, c in enumerate(_COLUMNS)]
    for row in [list(_COLUMNS), *rows]:
        sys.stdout.write("  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip() + "\n")


def _matching(ns: argparse.Namespace) -> list[dict[str, Any]] | None:
    """The indexed records `ns`'s filters select, after syncing the index; None
    (already reported) when there's no journal or it doesn't chain."""
    if not _have_journal():
        return None
    try:
        return host.audit_query(Query(
            since=ns.since, until=ns.until, type=ns.type, host=ns.host,
            bottle=ns.bottle, activation=ns.activation, bottled_agent=ns.agent,
            actor=ns.actor, outcome=ns.outcome, repository=ns.repository,
            correlation_id=ns.correlation_id, commit_sha=ns.commit, limit=ns.limit,
        ))
    except HostControlRefused as e:
        if e.code != 409:
            raise
        sys.stderr.write(f"{PROG}: {e.error}; run `{PROG} audit verify`\n")
        return None


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
    if not _have_journal():
        return 0
    try:
        count = host.audit_rebuild()
    except HostControlRefused as e:
        if e.code != 409:
            raise
        sys.stderr.write(f"{PROG}: {e.error}\n")
        return 1
    sys.stdout.write(f"indexed {count} record(s)\n")
    return 0


def _import(ns: argparse.Namespace) -> int:
    try:
        result = host.audit_import(str(Path(ns.segment).resolve()))
    except HostControlRefused as e:
        sys.stderr.write(f"{PROG}: can't import {ns.segment}: {e.error}\n")
        return 1
    where = result["chain"]
    if result["already_imported"]:
        sys.stdout.write(f"already imported into {where}\n")
    else:
        how = "extended" if result["extended"] else "new chain"
        sys.stdout.write(f"imported {result['records']} record(s) into {where} ({how})\n")
    return 0


def _delivery(ns: argparse.Namespace) -> int:
    config = host.set_host_config(HostConfig(audit_delivery=ns.mode), "audit_delivery") \
        if ns.mode else host.host_config()
    sys.stdout.write(f"{config.audit_delivery}\n")
    return 0


def _transcript_retention(ns: argparse.Namespace) -> int:
    if ns.days is not None and ns.days < 1:
        sys.stderr.write("transcript-retention: DAYS must be at least 1\n")
        return 2
    config = host.set_host_config(HostConfig(transcript_retention_days=ns.days),
                                  "transcript_retention_days") \
        if ns.days is not None else host.host_config()
    sys.stdout.write(f"{config.transcript_retention_days} days\n")
    return 0


def _retention(ns: argparse.Namespace) -> int:
    if (ns.days is not None and ns.days < 0) or (ns.max_gb is not None and ns.max_gb < 0):
        sys.stderr.write("retention: DAYS and GB must be 0 (no limit) or more\n")
        return 2
    retention = host.host_config().audit_retention
    if ns.days is not None or ns.max_gb is not None:
        retention = host.set_host_config(HostConfig(audit_retention=AuditRetention(
            days=retention.days if ns.days is None else ns.days,
            max_gb=retention.max_gb if ns.max_gb is None else ns.max_gb)),
            "audit_retention").audit_retention
    if retention.keeps_all:
        sys.stdout.write("keeping every segment\n")
        return 0
    limits = [f"{retention.days} days" if retention.days else "",
              f"{retention.max_gb} GiB" if retention.max_gb else ""]
    sys.stdout.write(f"keeping up to {' and '.join(limit for limit in limits if limit)}\n")
    return 0


def _forward(ns: argparse.Namespace) -> int:
    if ns.off or ns.url:
        forward = host.set_host_config(HostConfig(audit_forward=None if ns.off else AuditForward(
            url=ns.url, secret=ns.secret or "", format=ns.format)), "audit_forward").audit_forward
    else:
        forward = host.host_config().audit_forward
    if forward is None:
        sys.stdout.write("not forwarding\n")
    else:
        token = f", bearer token from secret {forward.secret}" if forward.secret else ""
        sys.stdout.write(f"forwarding {forward.format} to {forward.url}{token}\n")
    return 0


def tail_line(record: dict[str, Any]) -> str:
    """One request record as a line a person can follow."""
    if "firehose_dropped" in record:
        return f"... {record['firehose_dropped']} record(s) dropped"
    req = record.get("http_request") or {}
    url = req.get("url") or {}
    resp = record.get("http_response") or {}
    extra = (record.get("unmapped") or {}).get("bot_bottle") or {}
    bottle = (record.get("actor") or {}).get("app_name") or "-"
    target = f"{url.get('scheme') or 'https'}://{url.get('hostname')}:{url.get('port')}" \
             f"{url.get('path') or ''}"
    status, decision = resp.get("code", "-"), extra.get("decision", "")
    return f"{bottle}  {req.get('http_method', '?')} {target}  {status}  {decision}"


def _tail(ns: argparse.Namespace) -> int:
    # pylint: disable=import-outside-toplevel
    from ...control.hostctl import runner
    from ...control.hostctl.client import HostControlClient
    state = runner.running()
    if state is None:
        sys.stderr.write(f"{PROG}: the host controller isn't running; start it with "
                         f"`{PROG} up`\n")
        return 1
    try:
        for record in HostControlClient(state, runner.cli_token()).firehose(
                bottle=ns.bottle or "", host=ns.host or ""):
            sys.stdout.write((json.dumps(record) if ns.json else tail_line(record)) + "\n")
            sys.stdout.flush()
    except KeyboardInterrupt:
        return 0
    except runner.HostControlError as e:
        sys.stderr.write(f"{PROG}: {e}\n")
        return 1
    return 0


def _firehose(ns: argparse.Namespace) -> int:
    config = host.host_config()
    settings = config.firehose
    sinks = list(settings.sinks)
    if ns.clear:
        sinks = []
    if ns.remove is not None:
        if not 1 <= ns.remove <= len(sinks):
            sys.stderr.write(f"{PROG}: no sink {ns.remove}\n")
            return 1
        del sinks[ns.remove - 1]
    if ns.exec:
        sinks.append(FirehoseSink("exec", command=tuple(shlex.split(ns.exec))))
    if ns.https:
        sinks.append(FirehoseSink("https", url=ns.https, secret=ns.secret or ""))
    changed = dataclasses.replace(
        settings, sinks=tuple(sinks),
        request_bodies=settings.request_bodies if ns.request_bodies is None
        else ns.request_bodies == "on",
        response_bodies=settings.response_bodies if ns.response_bodies is None
        else ns.response_bodies == "on",
        max_body_bytes=settings.max_body_bytes if ns.max_body_bytes is None
        else ns.max_body_bytes)
    if changed != settings:
        changed = host.set_host_config(dataclasses.replace(config, firehose=changed),
                                       "firehose").firehose
    on = {True: "on", False: "off"}
    sys.stdout.write(f"request bodies {on[changed.request_bodies]}, response bodies "
                     f"{on[changed.response_bodies]}, up to {changed.max_body_bytes} bytes\n")
    if not changed.sinks:
        sys.stdout.write("no sinks (only `audit tail` sees the stream)\n")
    for i, sink in enumerate(changed.sinks, start=1):
        target = shlex.join(sink.command) if sink.type == "exec" else sink.url
        token = f" (bearer token from secret {sink.secret})" if sink.secret else ""
        sys.stdout.write(f"{i}. {sink.type}: {target}{token}\n")
    return 0


def _passkeys(ns: argparse.Namespace) -> int:
    return audit_passkeys.run(bool(ns.trust))


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
    passkeys = sub.add_parser(
        "passkeys", help="the passkeys that may unlock reading audit records from the console")
    passkeys.add_argument("--trust", action="store_true",
                          help="fetch them from the console and trust them once confirmed")
    passkeys.set_defaults(run=_passkeys)
    delivery = sub.add_parser(
        "delivery",
        help="show or set the default audit.delivery for bottles that don't set it")
    delivery.add_argument("mode", nargs="?", choices=AUDIT_DELIVERIES)
    delivery.set_defaults(run=_delivery)
    retention = sub.add_parser(
        "transcript-retention",
        help="show or set how many days ended sessions' transcript copies are kept")
    retention.add_argument("days", nargs="?", type=int)
    retention.set_defaults(run=_transcript_retention)
    journal_retention = sub.add_parser(
        "retention",
        help="show or set how much of the journal is kept; older segments are pruned")
    journal_retention.add_argument("--days", type=int,
                                   help="prune segments older than this (0: no limit)")
    journal_retention.add_argument("--max-gb", type=int, dest="max_gb",
                                   help="prune the oldest segments past this many GiB "
                                        "(0: no limit)")
    journal_retention.set_defaults(run=_retention)
    forward = sub.add_parser(
        "forward", help="show or set where the host controller forwards the journal")
    forward.add_argument("--url", help="an https endpoint that accepts NDJSON POSTs")
    forward.add_argument("--secret", help="name of the secrets-file entry sent as a bearer token")
    forward.add_argument("--format", choices=("native", "ocsf"), default="native")
    forward.add_argument("--off", action="store_true", help="stop forwarding")
    forward.set_defaults(run=_forward)
    tail = sub.add_parser("tail", help="follow every egress request as it happens")
    tail.add_argument("--bottle", help="only this bottle's requests")
    tail.add_argument("--host", help="only requests to this host")
    tail.add_argument("--json", action="store_true", help="each record as OCSF JSON")
    tail.set_defaults(run=_tail)
    firehose = sub.add_parser("firehose", help="show or set the request firehose's sinks")
    firehose.add_argument("--exec", metavar="COMMAND",
                          help="add a sink: a command whose stdin gets each record as JSON")
    firehose.add_argument("--https", metavar="URL", help="add a sink: an NDJSON endpoint")
    firehose.add_argument("--secret", help="for --https: the secrets-file entry sent as a "
                                           "bearer token")
    firehose.add_argument("--remove", type=int, metavar="N", help="remove sink N")
    firehose.add_argument("--clear", action="store_true", help="remove every sink")
    firehose.add_argument("--request-bodies", choices=("on", "off"))
    firehose.add_argument("--response-bodies", choices=("on", "off"))
    firehose.add_argument("--max-body-bytes", type=int)
    firehose.set_defaults(run=_firehose)
    ns = parser.parse_args(args)
    try:
        return ns.run(ns)
    except HostControlRefused as e:
        sys.stderr.write(f"{PROG} audit {ns.action}: {e.error}\n")
    except HostControlError as e:
        sys.stderr.write(f"{PROG} audit {ns.action}: {e}\n")
    return 1
