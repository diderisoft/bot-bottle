"""Unit tests for the orchestrator launch broker (PRD 0070)."""

from __future__ import annotations

import secrets
import unittest

from bot_bottle.orchestrator.broker import (
    BrokerAuthError,
    LaunchRequest,
    StubBroker,
    sign_request,
    verify_request,
)


class TestSignVerify(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = secrets.token_bytes(16)

    def test_launch_round_trip(self) -> None:
        req = LaunchRequest(
            op="launch", bottle_id="b1", source_ip="10.243.0.1",
            image_ref="sha256:abc", slot=3,
        )
        self.assertEqual(req, verify_request(sign_request(req, self.secret), self.secret))

    def test_teardown_round_trip(self) -> None:
        req = LaunchRequest(op="teardown", bottle_id="b1")
        got = verify_request(sign_request(req, self.secret), self.secret)
        self.assertEqual(("teardown", "b1"), (got.op, got.bottle_id))

    def test_wrong_secret_rejected(self) -> None:
        token = sign_request(LaunchRequest(op="launch", bottle_id="b1"), self.secret)
        with self.assertRaises(BrokerAuthError):
            verify_request(token, secrets.token_bytes(16))

    def test_tampered_payload_rejected(self) -> None:
        token = sign_request(LaunchRequest(op="launch", bottle_id="b1"), self.secret)
        header, payload, sig = token.split(".")
        flipped = payload[:-1] + ("A" if payload[-1] != "A" else "B")
        with self.assertRaises(BrokerAuthError):
            verify_request(f"{header}.{flipped}.{sig}", self.secret)

    def test_malformed_tokens_rejected(self) -> None:
        for bad in ("", "a.b", "a.b.c.d", "not-a-token"):
            with self.assertRaises(BrokerAuthError):
                verify_request(bad, self.secret)

    def test_unknown_op_rejected_despite_valid_signature(self) -> None:
        # A correctly-signed token whose op isn't in the allow-list must
        # still be refused — the schema guard is independent of provenance.
        token = sign_request(LaunchRequest(op="rm-rf", bottle_id="b1"), self.secret)
        with self.assertRaises(BrokerAuthError):
            verify_request(token, self.secret)


class TestStubBroker(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = secrets.token_bytes(16)
        self.broker = StubBroker(self.secret)

    def test_submit_launch_records_verified_request(self) -> None:
        req = LaunchRequest(op="launch", bottle_id="b1", source_ip="10.243.0.1")
        got = self.broker.submit(sign_request(req, self.secret))
        self.assertEqual(req, got)
        self.assertEqual(["b1"], [r.bottle_id for r in self.broker.launched])
        self.assertEqual([], self.broker.torn_down)

    def test_submit_teardown_records(self) -> None:
        self.broker.submit(
            sign_request(LaunchRequest(op="teardown", bottle_id="b1"), self.secret)
        )
        self.assertEqual(["b1"], [r.bottle_id for r in self.broker.torn_down])

    def test_submit_forged_token_is_fail_closed(self) -> None:
        forged = sign_request(
            LaunchRequest(op="launch", bottle_id="b1"), secrets.token_bytes(16)
        )
        with self.assertRaises(BrokerAuthError):
            self.broker.submit(forged)
        self.assertEqual([], self.broker.launched)  # nothing acted on


if __name__ == "__main__":
    unittest.main()
