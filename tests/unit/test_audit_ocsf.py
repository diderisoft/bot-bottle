"""Unit: journal records projected onto OCSF 1.9.0, checked against a vendored
subset of the schema (tests/unit/data/ocsf_1_9_0_subset.json, fetched from
schema.ocsf.io) so no runtime dependency is needed."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from bot_bottle.contract.audit import record_hash
from bot_bottle.control.hostctl.audit_ocsf import to_ocsf

_SCHEMA = json.loads((Path(__file__).parent / "data" / "ocsf_1_9_0_subset.json").read_text())
_CLASS_NAMES = {c["uid"]: name for name, c in _SCHEMA["classes"].items()}
# Typed object fields and the OCSF object each holds.
_OBJECTS = {"metadata": "metadata", "api": "api", "finding_info": "finding_info",
            "raw_data_hash": "fingerprint", "device": "device", "user": "user"}


def _record(kind: str, outcome: str = "success", **extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "v": 1, "id": f"id-{kind}", "type": kind, "epoch": 2, "seq": 7,
        "segment": "20261023T000000Z", "prev": "a" * 64, "hash": "",
        "ts_event": "2026-10-23T12:00:00.000Z", "ts_recorded": "2026-10-23T12:00:00.250Z",
        "ts_mono": 1.5, "producer": "egress", "host": "mac-studio-1",
        "engine": "bot-bottle/0.1.4+abc123", "bottle": "amber-fox",
        "bottled_agent": "amber-fox", "activation": "3f9a0c1d2e4b5a69",
        "manifest_digest": "sha256:" + "b" * 64, "actor": "bottled-agent:amber-fox",
        "action": kind, "resource": "registry.npmjs.org:443", "outcome": outcome,
        "sensitivity": "security", "correlation_id": None, "causation_id": "",
        "payload": {"method": "GET", "host": "registry.npmjs.org", "port": 443,
                    "src_ip": "10.243.0.9"},
        "untrusted": None,
    }
    record.update(extra)
    record["hash"] = record_hash(record["prev"], record)
    return record


_SAMPLES = [
    _record("egress.decision", "allowed"),
    _record("egress.decision", "blocked", payload={"method": "CONNECT", "host": "evil.example",
                                                   "port": 443, "reason": "not in allowlist"}),
    _record("egress.anomaly", "blocked", payload={"reason": "egress DLP: token in body"}),
    _record("egress.cutoff", "blocked", payload={"host": "x", "port": 443, "bytes": 10}),
    _record("forge.push_accepted", payload={"repository": "app", "ref": "refs/heads/main"}),
    _record("forge.push_rejected", "blocked",
            payload={"repository": "app", "ref": "refs/heads/main", "findings": 2}),
    _record("decision.proposed", "deferred", untrusted={"agent_rationale": "need it"}),
    _record("decision.resolved", "blocked"),
    _record("auth.token_minted", payload={"token_id": "sha256:abc"}),
    _record("auth.token_rejected", "blocked"),
    _record("auth.authz_denied", "blocked"),
    _record("hostctl.broker_launch"),
    _record("hostctl.broker_teardown"),
    _record("lifecycle.bottled_agent_start"),
    _record("lifecycle.bottled_agent_crash", "failure"),
    _record("audit.segment_open"),
    _record("audit.export_failed", "failure"),
    _record("audit.truncation_detected", "failure"),
]


class SchemaConformance(unittest.TestCase):
    def test_every_projection_is_a_valid_event_of_its_class(self) -> None:
        for record in _SAMPLES:
            with self.subTest(type=record["type"], outcome=record["outcome"]):
                event = to_ocsf(record)
                name = _CLASS_NAMES[event["class_uid"]]
                spec = _SCHEMA["classes"][name]
                self.assertEqual(spec["category_uid"], event["category_uid"])
                self.assertEqual(event["class_uid"] * 100 + event["activity_id"],
                                 event["type_uid"])
                for field in spec["required"]:
                    self.assertIn(field, event, f"{name} requires {field}")
                unknown = set(event) - set(spec["attributes"])
                self.assertEqual(set(), unknown, f"not attributes of {name}")
                for enum, allowed in spec["enums"].items():
                    if enum in event:
                        self.assertIn(event[enum], allowed, f"{enum} for {name}")
                for field, obj in _OBJECTS.items():
                    for required in _SCHEMA["objects"][obj]["required"] if field in event else ():
                        self.assertIn(required, event[field], f"{field} requires {required}")

    def test_the_chosen_classes(self) -> None:
        classes = {(r["type"], r["outcome"]): _CLASS_NAMES[to_ocsf(r)["class_uid"]]
                   for r in _SAMPLES}
        self.assertEqual("http_activity", classes[("egress.decision", "allowed")])
        self.assertEqual("detection_finding", classes[("egress.anomaly", "blocked")])
        self.assertEqual("detection_finding", classes[("forge.push_rejected", "blocked")])
        self.assertEqual("api_activity", classes[("forge.push_accepted", "success")])
        self.assertEqual("authentication", classes[("auth.token_minted", "success")])
        self.assertEqual("application_lifecycle",
                         classes[("lifecycle.bottled_agent_start", "success")])
        self.assertEqual("event_log_actvity", classes[("audit.segment_open", "success")])


class Mapping(unittest.TestCase):
    def test_raw_data_rehashes_to_the_record(self) -> None:
        record = _SAMPLES[0]
        event = to_ocsf(record)
        raw = json.loads(event["raw_data"])
        self.assertEqual(record["hash"], record_hash(raw["prev"], raw))
        self.assertEqual(record["hash"], event["unmapped"]["bot_bottle"]["chain"]["hash"])

    def test_claims_stay_under_unmapped(self) -> None:
        event = to_ocsf(_record("decision.proposed", "deferred",
                                untrusted={"agent_rationale": "trust me"}))
        self.assertEqual({"agent_rationale": "trust me"},
                         event["unmapped"]["bot_bottle"]["untrusted"])
        typed = {k: v for k, v in event.items() if k not in ("unmapped", "raw_data")}
        self.assertNotIn("trust me", json.dumps(typed))

    def test_outcomes_map_to_action_and_disposition(self) -> None:
        allowed = to_ocsf(_record("egress.decision", "allowed"))
        blocked = to_ocsf(_record("egress.decision", "blocked"))
        self.assertEqual((1, 1, 1), (allowed["action_id"], allowed["disposition_id"],
                                     allowed["status_id"]))
        self.assertEqual((2, 2, 2), (blocked["action_id"], blocked["disposition_id"],
                                     blocked["status_id"]))
        self.assertEqual(25, to_ocsf(_record("decision.resolved", "blocked"))["disposition_id"])

    def test_attribution_fields(self) -> None:
        event = to_ocsf(_SAMPLES[0])
        self.assertEqual("amber-fox", event["actor"]["app_name"])
        self.assertEqual("3f9a0c1d2e4b5a69", event["actor"]["session"]["uid"])
        self.assertEqual({"uid": "sha256:" + "b" * 64, "name": "manifest"}, event["policy"])
        self.assertEqual("0.1.4+abc123", event["metadata"]["product"]["version"])
        self.assertEqual("egress", event["metadata"]["product"]["feature"]["name"])

    def test_a_restricted_record_ships_only_the_envelope(self) -> None:
        event = to_ocsf(_record("egress.decision", "allowed", sensitivity="restricted",
                                untrusted={"reason": "x"}))
        self.assertNotIn("raw_data", event)
        self.assertNotIn("http_request", event)
        self.assertNotIn("payload", event["unmapped"]["bot_bottle"])
        self.assertNotIn("untrusted", event["unmapped"]["bot_bottle"])
        self.assertIn("hash", event["unmapped"]["bot_bottle"]["chain"])


if __name__ == "__main__":
    unittest.main()
