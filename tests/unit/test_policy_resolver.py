"""Unit tests for the gateway-side PolicyResolver (PRD 0070). HTTP mocked."""

from __future__ import annotations

import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from bot_bottle.gateway.policy_resolver import (
    ORCHESTRATOR_AUTH_HEADER,
    ORCHESTRATOR_AUTH_JWT_ENV,
    PolicyResolveError,
    PolicyResolver,
    orchestrator_auth_headers,
)

_URLOPEN = "bot_bottle.gateway.policy_resolver.urllib.request.urlopen"


def _resp(payload: object) -> MagicMock:
    """A urlopen() return value: a context manager whose read() yields JSON."""
    m = MagicMock()
    m.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return m


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x/resolve", code, "err", {}, None)  # type: ignore[arg-type]


class TestOrchestratorAuthHeaders(unittest.TestCase):
    def test_sends_the_gateway_jwt_when_configured(self) -> None:
        with patch.dict("os.environ", {ORCHESTRATOR_AUTH_JWT_ENV: "gateway.jwt.tok"}):
            self.assertEqual({ORCHESTRATOR_AUTH_HEADER: "gateway.jwt.tok"}, orchestrator_auth_headers())

    def test_sends_nothing_when_unset(self) -> None:
        import os
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop(ORCHESTRATOR_AUTH_JWT_ENV, None)
            self.assertEqual({}, orchestrator_auth_headers())


class TestPolicyResolver(unittest.TestCase):
    def setUp(self) -> None:
        self.r = PolicyResolver("http://orch:8080")

    def test_resolve_returns_policy(self) -> None:
        with patch(_URLOPEN, return_value=_resp({"bottle_id": "b1", "policy": "P"})):
            self.assertEqual("P", self.r.resolve("10.243.0.1", "tok"))

    def test_resolve_always_fetches_fresh(self) -> None:
        # No cache — every resolve hits the orchestrator so revocations /
        # policy changes are honored immediately.
        with patch(_URLOPEN, return_value=_resp({"policy": "P"})) as m:
            self.r.resolve("10.243.0.1", "tok")
            self.r.resolve("10.243.0.1", "tok")
        self.assertEqual(2, m.call_count)

    def test_unattributed_403_is_none_fail_closed(self) -> None:
        with patch(_URLOPEN, side_effect=_http_error(403)):
            self.assertIsNone(self.r.resolve("10.243.0.9", "tok"))

    def test_other_http_error_raises(self) -> None:
        with patch(_URLOPEN, side_effect=_http_error(500)):
            with self.assertRaises(PolicyResolveError):
                self.r.resolve("10.243.0.1", "tok")

    def test_unreachable_raises(self) -> None:
        with patch(_URLOPEN, side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(PolicyResolveError):
                self.r.resolve("10.243.0.1", "tok")

    def test_missing_policy_field_is_empty(self) -> None:
        with patch(_URLOPEN, return_value=_resp({"bottle_id": "b1"})):
            self.assertEqual("", self.r.resolve("10.243.0.1", "tok"))

    def test_posts_source_ip_and_token(self) -> None:
        with patch(_URLOPEN, return_value=_resp({"policy": "P"})) as m:
            self.r.resolve("10.243.0.7", "the-token")
        req = m.call_args.args[0]
        self.assertTrue(req.full_url.endswith("/resolve"))
        sent = json.loads(req.data)
        self.assertEqual("10.243.0.7", sent["source_ip"])
        self.assertEqual("the-token", sent["identity_token"])

    def test_resolve_without_token_sends_empty(self) -> None:
        with patch(_URLOPEN, return_value=_resp({"policy": "P"})) as m:
            self.r.resolve("10.243.0.7")  # token optional
        self.assertEqual("", json.loads(m.call_args.args[0].data)["identity_token"])

    def test_resolve_bottle_id_returns_id(self) -> None:
        with patch(_URLOPEN, return_value=_resp({"bottle_id": "b1", "policy": "P"})):
            self.assertEqual("b1", self.r.resolve_bottle_id("10.243.0.1", "tok"))

    def test_resolve_bottle_id_403_is_none_fail_closed(self) -> None:
        with patch(_URLOPEN, side_effect=_http_error(403)):
            self.assertIsNone(self.r.resolve_bottle_id("10.243.0.9", "tok"))

    def test_resolve_bottle_id_missing_field_is_none(self) -> None:
        with patch(_URLOPEN, return_value=_resp({"policy": "P"})):
            self.assertIsNone(self.r.resolve_bottle_id("10.243.0.1", "tok"))

    def test_resolve_bottle_id_error_raises(self) -> None:
        with patch(_URLOPEN, side_effect=_http_error(500)):
            with self.assertRaises(PolicyResolveError):
                self.r.resolve_bottle_id("10.243.0.1", "tok")

    def test_resolve_policy_and_bottle_id_one_call(self) -> None:
        payload = {"bottle_id": "b1", "policy": "P", "tokens": {"EGRESS_TOKEN_0": "s"}}
        with patch(_URLOPEN, return_value=_resp(payload)) as m:
            self.assertEqual(
                ("P", "b1", {"EGRESS_TOKEN_0": "s"}),
                self.r.resolve_policy_and_bottle_id("10.243.0.1", "t"),
            )
        self.assertEqual(1, m.call_count)  # policy + id + tokens from a single /resolve

    def test_resolve_policy_and_bottle_id_403_is_none_none_empty(self) -> None:
        with patch(_URLOPEN, side_effect=_http_error(403)):
            self.assertEqual((None, None, {}), self.r.resolve_policy_and_bottle_id("10.243.0.9"))

    def test_resolve_policy_and_bottle_id_error_raises(self) -> None:
        with patch(_URLOPEN, side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(PolicyResolveError):
                self.r.resolve_policy_and_bottle_id("10.243.0.1")

    # --- supervise agent RPCs (issue #469) ---------------------------------

    def test_propose_supervise_returns_id_and_posts_payload(self) -> None:
        with patch(_URLOPEN, return_value=_resp({"proposal_id": "p-7"})) as m:
            pid = self.r.propose_supervise(
                "10.243.0.7", "the-token",
                tool="egress-allow", proposed_file="routes:\n", justification="j",
            )
        self.assertEqual("p-7", pid)
        req = m.call_args.args[0]
        self.assertTrue(req.full_url.endswith("/supervise/propose"))
        sent = json.loads(req.data)
        self.assertEqual("10.243.0.7", sent["source_ip"])
        self.assertEqual("the-token", sent["identity_token"])
        self.assertEqual("egress-allow", sent["tool"])
        self.assertEqual("routes:\n", sent["proposed_file"])

    def test_propose_supervise_unattributed_is_none(self) -> None:
        with patch(_URLOPEN, side_effect=_http_error(403)):
            self.assertIsNone(self.r.propose_supervise(
                "10.9.9.9", "t", tool="egress-allow", proposed_file="x", justification="j"))

    def test_propose_supervise_missing_id_is_none(self) -> None:
        with patch(_URLOPEN, return_value=_resp({})):
            self.assertIsNone(self.r.propose_supervise(
                "10.243.0.1", "t", tool="egress-allow", proposed_file="x", justification="j"))

    def test_propose_supervise_unreachable_raises(self) -> None:
        with patch(_URLOPEN, side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(PolicyResolveError):
                self.r.propose_supervise(
                    "10.243.0.1", "t", tool="egress-allow", proposed_file="x", justification="j")

    def test_poll_supervise_returns_status(self) -> None:
        with patch(_URLOPEN, return_value=_resp(
            {"status": "approved", "notes": "ok", "final_file": None})
        ) as m:
            result = self.r.poll_supervise("10.243.0.7", "tok", "p-7")
        assert result is not None
        self.assertEqual("approved", result["status"])
        req = m.call_args.args[0]
        self.assertTrue(req.full_url.endswith("/supervise/poll"))
        self.assertEqual("p-7", json.loads(req.data)["proposal_id"])

    def test_poll_supervise_unattributed_is_none(self) -> None:
        with patch(_URLOPEN, side_effect=_http_error(403)):
            self.assertIsNone(self.r.poll_supervise("10.9.9.9", "t", "p-7"))

    def test_poll_supervise_unreachable_raises(self) -> None:
        with patch(_URLOPEN, side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(PolicyResolveError):
                self.r.poll_supervise("10.243.0.1", "t", "p-7")


if __name__ == "__main__":
    unittest.main()
