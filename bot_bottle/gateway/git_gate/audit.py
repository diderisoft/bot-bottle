"""Record a git-gate push decision: `python3 -m bot_bottle.gateway.git_gate.audit`.

The pre-receive hook runs this once per decision (a ref accepted and
forwarded, or a push refused), so it sends one event and exits, with no
queue to retry from. It never fails the push: a lost event is reported on
stderr and the hook goes on.

    audit <type> <outcome> <ref> [--report FILE | --findings N] [--detail TEXT]

`--report` is a gitleaks JSON report; its findings are counted, never copied.
`--findings 0` says the ref was scanned clean; with neither, it wasn't scanned.
The repository is the bare repo the hook runs in, and the bottle comes from
`GIT_GATE_BOTTLE`, which the HTTP backend sets from the calling bottle's
attributed namespace.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from ...contract.audit import AUDIT_EVENTS_PATH, AuditEvent, utc_iso
from ..policy_resolver import orchestrator_auth_headers

ORCHESTRATOR_URL_ENV = "BOT_BOTTLE_ORCHESTRATOR_URL"
BOTTLE_ENV = "GIT_GATE_BOTTLE"
TIMEOUT_SECONDS = 3.0
TYPES = ("forge.push_accepted", "forge.push_rejected")


def _findings(report: str) -> int | None:
    if not report:
        return None
    try:
        raw = json.loads(Path(report).read_text(encoding="utf-8") or "[]")
    except (OSError, ValueError):
        return None
    return len(raw) if isinstance(raw, list) else None


def repository(cwd: Path) -> str:
    """The bare repo's name, from the directory the hook runs in."""
    return cwd.name.removesuffix(".git")


def build_event(kind: str, outcome: str, ref: str, *, bottle: str, src_ip: str,
                repo: str, findings: int | None, detail: str, now: float) -> AuditEvent:
    if findings is None:
        gitleaks_result = "not_scanned"
    else:
        gitleaks_result = "findings" if findings else "clean"
    payload: dict[str, object] = {"repository": repo, "ref": ref,
                                  "gitleaks_result": gitleaks_result,
                                  "findings": findings or 0}
    if src_ip:
        payload["src_ip"] = src_ip
    if detail:
        payload["detail"] = detail
    return AuditEvent(
        type=kind, actor=f"bottled-agent:{bottle}" if bottle else "unattributed",
        action="git.push", outcome=outcome, sensitivity="security", ts_event=utc_iso(now),
        resource=f"{repo}:{ref}", bottle=bottle or None, bottled_agent=bottle or None,
        payload=payload,
    )


def send(url: str, event: AuditEvent) -> None:
    body = json.dumps({"producer": "git-gate", "event": event.to_json()}).encode()
    req = urllib.request.Request(
        url.rstrip("/") + AUDIT_EVENTS_PATH, data=body, method="POST",
        headers={"Content-Type": "application/json", **orchestrator_auth_headers()})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="git-gate-audit")
    parser.add_argument("type", choices=TYPES)
    parser.add_argument("outcome", choices=("success", "blocked", "failure"))
    parser.add_argument("ref")
    parser.add_argument("--report", default="")
    parser.add_argument("--findings", type=int, default=None)
    parser.add_argument("--detail", default="")
    ns = parser.parse_args(argv)
    url = os.environ.get(ORCHESTRATOR_URL_ENV, "").strip()
    if not url:
        return 0  # nowhere to send it
    event = build_event(
        ns.type, ns.outcome, ns.ref, bottle=os.environ.get(BOTTLE_ENV, ""),
        src_ip=os.environ.get("SUPERVISE_SOURCE_IP", ""), repo=repository(Path.cwd()),
        findings=_findings(ns.report) if ns.report else ns.findings, detail=ns.detail,
        now=time.time())
    try:
        send(url, event)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        sys.stderr.write(f"git-gate: couldn't record {ns.type} for {ns.ref}: {e}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
