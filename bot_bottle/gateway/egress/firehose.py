"""The egress proxy's side of the request firehose (PRD 0093).

When something is listening, each request is snapshotted as the agent sent
it, before the gateway injects a credential: `Authorization`,
`Proxy-Authorization` and `Cookie` are dropped whatever their value, the
bottle's provisioned secrets are scrubbed, and a body keeps its first
`max_body_bytes` plus the SHA-256 of the whole. When the request finishes
(answered, blocked or failed) the snapshot becomes one OCSF 1.9.0 HTTP
Activity record.

`FirehoseClient` batches records to the orchestrator, which passes them to the
host controller. Its reply says whether anything is consuming the stream and
what to capture; while nothing is, `wanted` is False and no snapshot is taken,
and the client asks again every `ASK_SECONDS`. The stream is lossy: a full
queue or a failed send drops records and counts them, and a request is never
slowed by it.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import typing
import urllib.error
import urllib.request
from collections import deque

from ...contract.firehose import FIREHOSE_PATH, Capture
from ..policy_resolver import orchestrator_auth_headers
from .dlp_detectors import redact_tokens

OCSF_VERSION = "1.9.0"
STRIPPED_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})
_HTTP_METHODS = {"CONNECT": 1, "DELETE": 2, "GET": 3, "HEAD": 4, "OPTIONS": 5,
                 "POST": 6, "PUT": 7, "TRACE": 8, "PATCH": 9}
# decision -> (action_id, disposition_id, status_id)
_DECISIONS = {"allowed": (1, 1, 1), "redacted": (4, 11, 1), "blocked": (2, 2, 2),
              "failed": (1, 1, 2)}
_SNAPSHOT_KEY = "bot_bottle.firehose"

QUEUE_LIMIT = 5000
BATCH_RECORDS = 500
FLUSH_SECONDS = 1.0
ASK_SECONDS = 30.0
TIMEOUT_SECONDS = 5.0


def body_capture(raw: bytes, limit: int, env: typing.Mapping[str, str]) -> dict[str, object]:
    """The first `limit` bytes as text, secrets scrubbed; a longer body is
    marked truncated and carries the SHA-256 of the whole."""
    out: dict[str, object] = {
        "text": redact_tokens(raw[:limit].decode("utf-8", errors="replace"), env=env),
        "bytes": len(raw),
    }
    if len(raw) > limit:
        out["truncated"] = True
        out["sha256"] = hashlib.sha256(raw).hexdigest()
    return out


def snapshot(flow: typing.Any, env: typing.Mapping[str, str],
             capture: Capture) -> dict[str, object]:
    """The request as the agent sent it, made safe to stream."""
    req = flow.request
    path, _, query = str(req.path).partition("?")
    snap: dict[str, object] = {
        "method": req.method, "scheme": getattr(req, "scheme", "") or "",
        "host": req.pretty_host, "port": req.port,
        "path": redact_tokens(path, env=env), "query": redact_tokens(query, env=env),
        "headers": [{"name": k, "value": redact_tokens(v, env=env)}
                    for k, v in req.headers.items() if k.lower() not in STRIPPED_HEADERS],
        "started": time.monotonic(),
    }
    if capture.request_bodies:
        raw = req.get_content(strict=False) if hasattr(req, "get_content") else \
            (req.get_text(strict=False) or "").encode("utf-8")
        snap["body"] = body_capture(raw or b"", capture.max_body_bytes, env)
    return snap


def http_activity(snap: typing.Mapping[str, typing.Any], *, slug: str, src_ip: str,
                  decision: str, reason: str = "", status: int | None = None,
                  response_body: dict[str, object] | None = None,
                  response_bytes: int | None = None) -> dict[str, object]:
    """One OCSF 1.9.0 HTTP Activity record for a finished request."""
    action_id, disposition_id, status_id = _DECISIONS.get(decision, (99, 99, 99))
    activity_id = _HTTP_METHODS.get(str(snap.get("method", "")).upper(), 99)
    now_ms = int(time.time() * 1000)
    record: dict[str, typing.Any] = {
        "class_uid": 4002, "category_uid": 4, "activity_id": activity_id,
        "type_uid": 4002 * 100 + activity_id, "time": now_ms,
        "severity_id": 2 if decision in ("blocked", "failed") else 1,
        "metadata": {"version": OCSF_VERSION, "profiles": ["security_control"],
                     "product": {"name": "bot-bottle", "vendor_name": "bot-bottle",
                                 "feature": {"name": "firehose"}}},
        "http_request": {
            "http_method": snap.get("method"),
            "url": {"scheme": snap.get("scheme"), "hostname": snap.get("host"),
                    "port": snap.get("port"), "path": snap.get("path"),
                    "query_string": snap.get("query")},
            "http_headers": snap.get("headers", []),
        },
        "dst_endpoint": {"hostname": snap.get("host"), "port": snap.get("port")},
        "src_endpoint": {"ip": src_ip},
        "actor": {"app_name": slug},
        "action_id": action_id, "disposition_id": disposition_id, "status_id": status_id,
        "duration": int((time.monotonic() - float(snap.get("started", time.monotonic())))
                        * 1000),
    }
    if status is not None:
        record["http_response"] = {"code": status, "length": response_bytes}
    unmapped: dict[str, object] = {"decision": decision}
    if reason:
        unmapped["reason"] = reason
    if "body" in snap:
        unmapped["request_body"] = snap["body"]
    if response_body is not None:
        unmapped["response_body"] = response_body
    record["unmapped"] = {"bot_bottle": unmapped}
    return record


class FirehoseClient:
    """Batches records to the orchestrator and keeps its latest `Capture`."""

    def __init__(self, orchestrator_url: str, *, limit: int = QUEUE_LIMIT) -> None:
        self._url = orchestrator_url.rstrip("/") + FIREHOSE_PATH
        self._limit = limit
        self._records: deque[dict[str, object]] = deque()
        self._dropped = 0
        self._capture = Capture(wanted=False)
        self._asked = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="firehose")

    @property
    def capture(self) -> Capture:
        return self._capture

    def start(self) -> "FirehoseClient":
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()

    def submit(self, record: dict[str, object]) -> None:
        with self._lock:
            if len(self._records) >= self._limit:
                self._records.popleft()
                self._dropped += 1
            self._records.append(record)

    def flush(self) -> None:
        """Send what's queued, or ask whether to start capturing."""
        with self._lock:
            batch = [self._records.popleft()
                     for _ in range(min(BATCH_RECORDS, len(self._records)))]
            dropped, self._dropped = self._dropped, 0
        if not batch and not dropped and (
                self._capture.wanted or time.monotonic() - self._asked < ASK_SECONDS):
            return
        body = json.dumps({"records": batch, "dropped": dropped}).encode()
        req = urllib.request.Request(
            self._url, data=body, method="POST",
            headers={"Content-Type": "application/json", **orchestrator_auth_headers()})
        self._asked = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                self._capture = Capture.from_json(json.loads(resp.read() or b"{}"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            # Lossy: the batch is gone, and capture pauses until the next ask.
            with self._lock:
                self._dropped += len(batch) + dropped
            self._capture = Capture(wanted=False)

    def _run(self) -> None:
        while not self._stop.wait(FLUSH_SECONDS):
            self.flush()


class EgressFirehose:
    """The addon's hooks into the firehose."""

    def __init__(self, client: FirehoseClient) -> None:
        self._client = client

    def begin(self, flow: typing.Any, env: typing.Mapping[str, str]) -> None:
        capture = self._client.capture
        meta = getattr(flow, "metadata", None)
        if capture.wanted and isinstance(meta, dict):
            meta[_SNAPSHOT_KEY] = {"snap": snapshot(flow, env, capture), "capture": capture,
                                   "env": env}

    def mark(self, flow: typing.Any, decision: str) -> None:
        """Note a decision that isn't the default `allowed` (e.g. redacted)."""
        entry = getattr(flow, "metadata", {}).get(_SNAPSHOT_KEY)
        if entry is not None:
            entry["decision"] = decision

    def end(self, flow: typing.Any, slug: str, decision: str = "", reason: str = "") -> None:
        """Emit the request's record, once. `decision` overrides a `mark`."""
        meta = getattr(flow, "metadata", None)
        entry = meta.pop(_SNAPSHOT_KEY, None) if isinstance(meta, dict) else None
        if entry is None:
            return
        conn = flow.client_conn
        src_ip = conn.peername[0] if conn is not None and conn.peername else ""
        resp = flow.response
        response_body, response_bytes = None, None
        if resp is not None:
            raw = resp.get_content(strict=False) if hasattr(resp, "get_content") else None
            raw = raw if isinstance(raw, bytes) else (resp.get_text(strict=False) or "").encode()
            response_bytes = len(raw)
            if entry["capture"].response_bodies:
                response_body = body_capture(raw, entry["capture"].max_body_bytes,
                                             entry["env"])
        self._client.submit(http_activity(
            entry["snap"], slug=slug, src_ip=src_ip,
            decision=decision or entry.get("decision", "allowed"), reason=reason,
            status=getattr(resp, "status_code", None) if resp is not None else None,
            response_body=response_body, response_bytes=response_bytes))


__all__ = ["EgressFirehose", "FirehoseClient", "body_capture", "http_activity", "snapshot"]
