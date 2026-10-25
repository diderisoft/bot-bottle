"""Project a journal record onto OCSF 1.9.0, deterministically from the record
alone. Typed fields are for querying; `raw_data` carries the journal line so a
receiver can check the chain. `untrusted` goes only under
`unmapped.bot_bottle.untrusted`. A `restricted` record ships only the envelope
and chain fields.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from ...contract.audit import canonical

OCSF_VERSION = "1.9.0"
PROFILES = ["security_control", "host"]

HTTP_ACTIVITY, NETWORK_ACTIVITY = 4002, 4001
DETECTION_FINDING = 2004
API_ACTIVITY, APPLICATION_LIFECYCLE = 6003, 6002
AUTHENTICATION = 3002
EVENT_LOG_ACTIVITY = 1008

_HTTP_METHODS = {"CONNECT": 1, "DELETE": 2, "GET": 3, "HEAD": 4, "OPTIONS": 5,
                 "POST": 6, "PUT": 7, "TRACE": 8, "PATCH": 9}
# outcome -> (action_id, disposition_id, status_id); None omits it.
_OUTCOMES: dict[str, tuple[int | None, int | None, int]] = {
    "allowed": (1, 1, 1),
    "blocked": (2, 2, 2),
    "deferred": (99, 14, 99),
    "success": (None, None, 1),
    "failure": (None, None, 2),
}
_SHA256 = 3
_OTHER = 99
_HAS_SRC_ENDPOINT = (HTTP_ACTIVITY, NETWORK_ACTIVITY, API_ACTIVITY, AUTHENTICATION)


def _ms(iso: str) -> int | None:
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


# type -> (class_uid, activity_id) for types not depending on payload; unlisted
# types are API Activity: Create.
_BY_TYPE: dict[str, tuple[int, int]] = {
    "egress.cutoff": (NETWORK_ACTIVITY, 2),  # Close
    "egress.anomaly": (DETECTION_FINDING, 1),  # Create
    "audit.verify_failed": (DETECTION_FINDING, 1),
    "audit.truncation_detected": (DETECTION_FINDING, 1),
    "auth.token_minted": (AUTHENTICATION, 4),  # Service Ticket Request
    "auth.token_rejected": (AUTHENTICATION, 1),  # Logon
    "lifecycle.bottled_agent_start": (APPLICATION_LIFECYCLE, 3),  # Start
    "lifecycle.bottled_agent_stop": (APPLICATION_LIFECYCLE, 4),  # Stop
    "lifecycle.bottled_agent_crash": (APPLICATION_LIFECYCLE, 4),
    "audit.segment_open": (EVENT_LOG_ACTIVITY, 5),  # Rotate
    "audit.export_failed": (EVENT_LOG_ACTIVITY, 3),  # Export
    "decision.resolved": (API_ACTIVITY, 3),  # Update
    "decision.late": (API_ACTIVITY, 3),
    "hostctl.broker_teardown": (API_ACTIVITY, 4),  # Delete
    "auth.authz_denied": (API_ACTIVITY, _OTHER),
}


def _class_and_activity(record: dict[str, Any]) -> tuple[int, int]:
    kind = str(record.get("type", ""))
    payload = record.get("payload") or {}
    if kind in ("egress.request", "egress.decision"):
        return HTTP_ACTIVITY, _HTTP_METHODS.get(str(payload.get("method", "")).upper(), _OTHER)
    if kind == "forge.push_rejected" and int(payload.get("findings") or 0) > 0:
        return DETECTION_FINDING, 1
    return _BY_TYPE.get(kind, (API_ACTIVITY, 1))


def _severity(class_uid: int, record: dict[str, Any]) -> int:
    if record.get("type") in ("audit.verify_failed", "audit.truncation_detected"):
        return 4  # High
    if class_uid == DETECTION_FINDING:
        return 3  # Medium
    return 2 if record.get("outcome") in ("blocked", "failure") else 1  # Low / Informational


def _message(record: dict[str, Any]) -> str:
    """A short line from trusted fields only."""
    parts = [str(record.get("outcome", "")), str(record.get("action", ""))]
    if record.get("resource"):
        parts.append(str(record["resource"]))
    if record.get("bottle"):
        parts.append(f"from {record['bottle']}")
    return " ".join(p for p in parts if p)


def _typed_fields(class_uid: int, record: dict[str, Any],
                  payload: dict[str, Any]) -> dict[str, Any]:
    """The class's own fields, from host-established record fields."""
    out: dict[str, Any] = {}
    if payload.get("src_ip") and class_uid in _HAS_SRC_ENDPOINT:
        out["src_endpoint"] = {"ip": payload["src_ip"]}
    if class_uid in (HTTP_ACTIVITY, NETWORK_ACTIVITY) and payload.get("host"):
        out["dst_endpoint"] = {"hostname": payload["host"], "port": payload.get("port")}
        if class_uid == HTTP_ACTIVITY:
            out["http_request"] = {
                "http_method": payload.get("method"),
                "url": {"hostname": payload["host"], "port": payload.get("port")},
            }
    if class_uid == DETECTION_FINDING:
        reason = payload.get("reason") or payload.get("detail") or ""
        out["finding_info"] = {"uid": record.get("id"),
                               "title": f"{record.get('type')}: {reason}".rstrip(": ")}
    if class_uid == API_ACTIVITY:
        out["api"] = {"operation": record.get("action")}
        out.setdefault("src_endpoint", {"hostname": record.get("host")})
    if class_uid == AUTHENTICATION:
        out["user"] = {"uid": record.get("bottled_agent") or record.get("actor")}
    if class_uid == APPLICATION_LIFECYCLE:
        out["app"] = {"name": record.get("bottled_agent") or record.get("bottle"),
                      "uid": record.get("activation")}
    if payload.get("repository"):
        out["resources"] = [{"type": "git_repository", "name": payload["repository"],
                             "uid": payload.get("commit_sha") or payload.get("ref")}]
    return out


def _outcome_fields(record: dict[str, Any]) -> dict[str, Any]:
    action_id, disposition_id, status_id = _OUTCOMES.get(
        str(record.get("outcome")), (None, None, 0))
    if record.get("type") == "decision.resolved":
        disposition_id = 25 if record.get("outcome") == "blocked" else 8  # Rejected / Approved
    out: dict[str, Any] = {"status_id": status_id}
    if action_id is not None:
        out["action_id"] = action_id
    if disposition_id is not None:
        out["disposition_id"] = disposition_id
    return out


def to_ocsf(record: dict[str, Any]) -> dict[str, Any]:
    """The OCSF 1.9.0 event for one journal record."""
    class_uid, activity_id = _class_and_activity(record)
    restricted = record.get("sensitivity") == "restricted"
    payload: dict[str, Any] = {} if restricted else dict(record.get("payload") or {})
    engine = str(record.get("engine", ""))
    metadata: dict[str, Any] = {
        "version": OCSF_VERSION,
        "profiles": PROFILES,
        "product": {"name": "bot-bottle", "vendor_name": "bot-bottle",
                    "version": engine.partition("/")[2] or engine,
                    "feature": {"name": record.get("producer")}},
        "uid": record.get("id"),
        "original_event_uid": record.get("id"),
        "event_code": record.get("type"),
        "sequence": record.get("seq"),
    }
    if record.get("correlation_id"):
        metadata["correlation_uid"] = record["correlation_id"]
    logged = _ms(str(record.get("ts_recorded", "")))
    if logged is not None:
        metadata["logged_time"] = logged

    event: dict[str, Any] = {
        "class_uid": class_uid,
        "category_uid": class_uid // 1000,
        "activity_id": activity_id,
        "type_uid": class_uid * 100 + activity_id,
        "time": _ms(str(record.get("ts_event", ""))) or logged or 0,
        "severity_id": _severity(class_uid, record),
        "message": _message(record),
        "metadata": metadata,
        "device": {"hostname": record.get("host"), "type_id": 0},
        "actor": {"user": {"uid": record.get("actor")}},
        **_outcome_fields(record),
        **_typed_fields(class_uid, record, payload),
    }
    if record.get("activation"):
        event["actor"]["session"] = {"uid": record["activation"]}
    if record.get("bottle"):
        # The bottle is the acting application.
        event["actor"]["app_name"] = record["bottle"]
    if record.get("manifest_digest"):
        event["policy"] = {"uid": record["manifest_digest"], "name": "manifest"}

    unmapped: dict[str, Any] = {
        "chain": {k: record.get(k) for k in ("v", "epoch", "segment", "prev", "hash")},
        "sensitivity": record.get("sensitivity"),
        "bottle": record.get("bottle"),
    }
    if record.get("causation_id"):
        unmapped["causation_id"] = record["causation_id"]
    if payload:
        unmapped["payload"] = payload
    if record.get("untrusted") and not restricted:
        unmapped["untrusted"] = record["untrusted"]
    event["unmapped"] = {"bot_bottle": unmapped}

    if not restricted:
        raw = canonical(record)
        event["raw_data"] = raw
        event["raw_data_hash"] = {"algorithm_id": _SHA256,
                                  "value": hashlib.sha256(raw.encode("utf-8")).hexdigest()}
    return event


__all__ = ["OCSF_VERSION", "to_ocsf"]
