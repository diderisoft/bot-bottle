"""Unit: the audit-event envelope and hash chain."""

from __future__ import annotations

import unittest

from bot_bottle.contract.audit import AuditEvent, canonical, record_hash


# The audit spec's normative vectors: an implementation is conformant iff it
# reproduces these bytes and hashes.
_RECORD0 = {"hash": "", "id": "11111111-1111-4111-8111-111111111111",
            "prev": "", "seq": 0, "type": "audit.segment_open"}
_CANONICAL0 = ('{"hash":"","id":"11111111-1111-4111-8111-111111111111",'
               '"prev":"","seq":0,"type":"audit.segment_open"}')
_HASH0 = "942ea5729bcac6efdbdea942396bfa574ab0d6ebf5615402595359422f2aeb83"
_HASH1 = "bc082347680405fee50b60a9c304611aa026950b15d869b7e3ae56e1c451b856"
_TAMPERED0 = "53ddfcfa2244567371639b0bb66c93273fc270c2e1784e42ad67341a14bf2482"


def _record1() -> dict[str, object]:
    return {"hash": "", "id": "22222222-2222-4222-8222-222222222222",
            "prev": _HASH0, "seq": 1, "type": "lifecycle.bottled_agent_start"}


class ChainVectors(unittest.TestCase):
    def test_canonical_bytes(self) -> None:
        self.assertEqual(canonical(_RECORD0), _CANONICAL0)

    def test_genesis_hash(self) -> None:
        self.assertEqual(record_hash("", _RECORD0), _HASH0)

    def test_second_record_chains_onto_the_first(self) -> None:
        self.assertEqual(record_hash(_HASH0, _record1()), _HASH1)

    def test_a_tampered_record_no_longer_matches_the_next_prev(self) -> None:
        tampered = {**_RECORD0, "type": "audit.tampered"}
        digest = record_hash("", tampered)
        self.assertEqual(digest, _TAMPERED0)
        self.assertNotEqual(digest, _record1()["prev"])

    def test_the_hash_field_is_ignored_when_hashing(self) -> None:
        """A sealed record re-hashes to its own `hash`."""
        sealed = {**_RECORD0, "hash": _HASH0}
        self.assertEqual(record_hash("", sealed), _HASH0)


def _event(**overrides: object) -> AuditEvent:
    base: dict[str, object] = {
        "type": "egress.decision", "actor": "bottled-agent:amber-fox-12",
        "action": "egress.connect", "outcome": "blocked", "sensitivity": "security",
        "ts_event": "2026-07-26T18:22:04.061Z"}
    base.update(overrides)
    return AuditEvent(**base)  # type: ignore[arg-type]


class Envelope(unittest.TestCase):
    def test_round_trips_through_json(self) -> None:
        event = _event(payload={"route_id": 4}, untrusted={"reason": "npm"})
        self.assertEqual(AuditEvent.from_json(event.to_json()), event)

    def test_each_event_gets_its_own_id(self) -> None:
        self.assertNotEqual(_event().id, _event().id)

    def test_an_unknown_top_level_field_is_refused(self) -> None:
        """A producer must not be able to add a field that looks host-set."""
        body = {**_event().to_json(), "host": "spoofed"}
        with self.assertRaises(ValueError):
            AuditEvent.from_json(body)

    def test_an_unknown_outcome_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            _event(outcome="maybe")

    def test_an_unknown_sensitivity_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            _event(sensitivity="secret")

    def test_an_undotted_type_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            _event(type="decision")


if __name__ == "__main__":
    unittest.main()
