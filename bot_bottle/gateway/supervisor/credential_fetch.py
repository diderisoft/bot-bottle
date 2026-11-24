"""`GET /bb-credential/<label>`: hand an attributed agent a readable credential.

The supervisor asks the orchestrator for the values of a credential the
bottle declared `agent_can_read`, attributed by the caller's source IP and
identity token, and records a best-effort `credential.fetched` audit event.
It fails closed: anything but a clean answer is an error status.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any

PATH_PREFIX = "/bb-credential/"


def _post(url: str, body: dict[str, Any], timeout: float) -> Any:
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
    )
    return urllib.request.urlopen(req, timeout=timeout)  # pylint: disable=consider-using-with


def _audit(orch_url: str, source_ip: str, label: str) -> None:
    try:
        _post(f"{orch_url}/audit/events", {
            "producer": "supervise",
            "event": {"kind": "credential.fetched", "label": label, "src_ip": source_ip},
        }, timeout=2).close()
    except Exception:  # pylint: disable=broad-exception-caught
        pass  # audit is best-effort; don't fail the credential serve


def fetch(path: str, source_ip: str, identity_token: str,
          orch_url: str) -> tuple[int, dict[str, Any]]:
    """The status and JSON body to answer `GET <path>` with."""
    label = path[len(PATH_PREFIX):]
    if not label:
        return 400, {"error": "missing label"}
    if not orch_url:
        return 503, {"error": "orchestrator not configured"}
    orch_url = orch_url.rstrip("/")
    try:
        with _post(f"{orch_url}/credentials/fetch", {
            "source_ip": source_ip, "identity_token": identity_token, "label": label,
        }, timeout=5) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 404, {"error": "credential not found or not readable"}
        return 503, {"error": f"orchestrator error: {e.code}"}
    except Exception as e:  # pylint: disable=broad-exception-caught
        sys.stderr.write(f"bb-credential: orchestrator fetch failed: {e}\n")
        return 503, {"error": "orchestrator unavailable"}
    _audit(orch_url, source_ip, label)
    return 200, result
