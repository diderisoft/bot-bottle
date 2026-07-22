"""Unit: host-side orchestrator control-plane client (PRD 0070). HTTP mocked."""

from __future__ import annotations

import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from bot_bottle.orchestrator.client import (
    OrchestratorClient,
    OrchestratorClientError,
    RegisteredBottle,
)

_URLOPEN = "bot_bottle.orchestrator.client.urllib.request.urlopen"


def _resp(status: int, payload: object) -> MagicMock:
    m = MagicMock()
    inner = m.__enter__.return_value
    inner.status = status
    inner.read.return_value = json.dumps(payload).encode()
    return m


def _http_error(code: int, payload: object = None) -> urllib.error.HTTPError:
    del payload  # the client tolerates an empty error body; keep the signature
    return urllib.error.HTTPError("http://x", code, "err", {}, None)  # type: ignore[arg-type]


class TestRegister(unittest.TestCase):
    def setUp(self) -> None:
        self.c = OrchestratorClient("http://orch:8080")

    def test_register_returns_id_and_token(self) -> None:
        with patch(_URLOPEN, return_value=_resp(201, {"bottle_id": "b1", "identity_token": "tok"})):
            got = self.c.register_bottle("10.0.0.2", policy="routes: []\n", metadata="{}")
        self.assertEqual(RegisteredBottle("b1", "tok"), got)

    def test_register_posts_source_ip_and_policy(self) -> None:
        with patch(_URLOPEN, return_value=_resp(201, {"bottle_id": "b", "identity_token": "t"})) as m:
            self.c.register_bottle("10.0.0.9", policy="P", metadata="M", image_ref="img")
        sent = json.loads(m.call_args.args[0].data)
        self.assertEqual("10.0.0.9", sent["source_ip"])
        self.assertEqual("P", sent["policy"])
        self.assertEqual("img", sent["image_ref"])

    def test_register_missing_fields_raises(self) -> None:
        with patch(_URLOPEN, return_value=_resp(201, {"bottle_id": "b1"})):
            with self.assertRaises(OrchestratorClientError):
                self.c.register_bottle("10.0.0.2")

    def test_register_non_2xx_raises(self) -> None:
        with patch(_URLOPEN, side_effect=_http_error(400, {"error": "bad"})):
            with self.assertRaises(OrchestratorClientError):
                self.c.register_bottle("")


class TestTeardown(unittest.TestCase):
    def setUp(self) -> None:
        self.c = OrchestratorClient("http://orch:8080")

    def test_teardown_true_on_success(self) -> None:
        with patch(_URLOPEN, return_value=_resp(200, {"torn_down": True})):
            self.assertTrue(self.c.teardown_bottle("b1"))

    def test_teardown_false_on_404(self) -> None:
        # Idempotent: an already-gone bottle is a clean no-op.
        with patch(_URLOPEN, side_effect=_http_error(404)):
            self.assertFalse(self.c.teardown_bottle("gone"))

    def test_teardown_uses_delete(self) -> None:
        with patch(_URLOPEN, return_value=_resp(200, {"torn_down": True})) as m:
            self.c.teardown_bottle("b1")
        self.assertEqual("DELETE", m.call_args.args[0].get_method())


class TestReprovisionGateway(unittest.TestCase):
    def setUp(self) -> None:
        self.c = OrchestratorClient("http://orch:8080")

    def test_success_posts_key(self) -> None:
        with patch(_URLOPEN, return_value=_resp(200, {"reprovisioned": True})) as opened:
            self.assertTrue(self.c.reprovision_gateway("b1", "key"))
        request = opened.call_args.args[0]
        self.assertEqual("POST", request.get_method())
        self.assertEqual({"env_var_secret": "key"}, json.loads(request.data))

    def test_missing_stored_secret_is_false(self) -> None:
        with patch(_URLOPEN, side_effect=_http_error(404)):
            self.assertFalse(self.c.reprovision_gateway("b1", "key"))

    def test_other_status_raises(self) -> None:
        with patch(_URLOPEN, side_effect=_http_error(400)):
            with self.assertRaises(OrchestratorClientError):
                self.c.reprovision_gateway("b1", "key")


class TestHealthAndPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.c = OrchestratorClient("http://orch:8080")

    def test_health_true_on_200(self) -> None:
        with patch(_URLOPEN, return_value=_resp(200, {"status": "ok"})):
            self.assertTrue(self.c.health())

    def test_health_false_when_unreachable(self) -> None:
        with patch(_URLOPEN, side_effect=urllib.error.URLError("refused")):
            self.assertFalse(self.c.health())

    def test_set_policy_false_on_404(self) -> None:
        with patch(_URLOPEN, side_effect=_http_error(404)):
            self.assertFalse(self.c.set_policy("gone", "P"))

    def test_set_policy_true_on_success(self) -> None:
        with patch(_URLOPEN, return_value=_resp(200, {"updated": True})):
            self.assertTrue(self.c.set_policy("b1", "P"))

    def test_unreachable_raises(self) -> None:
        with patch(_URLOPEN, side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(OrchestratorClientError):
                self.c.register_bottle("10.0.0.2")


if __name__ == "__main__":
    unittest.main()


class TestReconcile(unittest.TestCase):
    def setUp(self) -> None:
        self.c = OrchestratorClient("http://orch:8080")

    def test_posts_live_ips_and_returns_reaped(self) -> None:
        with patch(_URLOPEN, return_value=_resp(200, {"reaped": ["b1", "b2"]})) as m:
            got = self.c.reconcile(["10.0.0.2", "10.0.0.3"])
        self.assertEqual(["b1", "b2"], got)
        sent = json.loads(m.call_args.args[0].data)
        self.assertEqual(["10.0.0.2", "10.0.0.3"], sent["live_source_ips"])
        self.assertNotIn("grace_seconds", sent)  # omitted -> server default

    def test_grace_seconds_is_forwarded_when_given(self) -> None:
        with patch(_URLOPEN, return_value=_resp(200, {"reaped": []})) as m:
            self.c.reconcile([], grace_seconds=30)
        self.assertEqual(30, json.loads(m.call_args.args[0].data)["grace_seconds"])

    def test_malformed_reaped_is_tolerated(self) -> None:
        with patch(_URLOPEN, return_value=_resp(200, {"reaped": ["ok", 5, None]})):
            self.assertEqual(["ok"], self.c.reconcile([]))
        with patch(_URLOPEN, return_value=_resp(200, {})):
            self.assertEqual([], self.c.reconcile([]))

    def test_error_status_raises(self) -> None:
        with patch(_URLOPEN, side_effect=_http_error(500)):
            with self.assertRaises(OrchestratorClientError):
                self.c.reconcile([])
