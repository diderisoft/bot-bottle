"""audit: check and search the host's audit journal."""

from __future__ import annotations

import argparse
import sys

from ...control.hostctl.audit_verify import verify
from ...paths import host_audit_dir
from ..constants import PROG


def _verify(_ns: argparse.Namespace) -> int:
    directory = host_audit_dir()
    if not directory.is_dir():
        sys.stdout.write(f"no audit journal at {directory}\n")
        return 0
    report = verify(directory)
    for problem in report.problems:
        sys.stdout.write(f"{problem}\n")
    if report.starts_after:
        sys.stdout.write("journal starts mid-chain (older segments were removed)\n")
    verdict = "ok" if report.ok else f"BROKEN ({len(report.breaks)} break(s))"
    sys.stdout.write(f"{verdict}: {report.records} record(s) in {report.segments} "
                     f"segment(s), head {report.head[:16] or '-'}\n")
    return 0 if report.ok else 1


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
    ns = parser.parse_args(args)
    return ns.run(ns)
