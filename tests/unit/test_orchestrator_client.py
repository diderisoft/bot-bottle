"""Unit: host-side orchestrator control-plane client (PRD 0070). HTTP mocked."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from bot_bottle.contract.auth import ROLE_CLI, verify
from bot_bottle.control.client import (
    ORCHESTRATOR_KEY,
    AlreadyAnswered,
    OrchestratorClient,
    OrchestratorClientError,
    RegisteredBottle,
    BackendProbeFailure,
    _host_auth_token,
    _probe_failure,
    discover_orchestrator_url,
    discover_orchestrator_urls,
    pending_everywhere,
)

_URLOPEN = "bot_bottle.control.client.urllib.request.urlopen"


class TestImportBoundary(unittest.TestCase):
    def test_host_client_does_not_import_server_dependencies(self) -> None:
        script = """
import importlib.abc
import sys

class BlockServerDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in {"fastapi", "uvicorn"}:
            raise ImportError(f"host import reached {fullname}")
        return None

sys.meta_path.insert(0, BlockServerDependencies())
import bot_bottle.control.client
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)


class TestHostAuthToken(unittest.TestCase):
    def test_mints_a_cli_token_from_the_host_key(self) -> None:
        # The CLI mints its `cli` token from the control-plane trust domain's
        # host-canonical key (#476) — patch the key file read underneath it.
        with patch("bot_bottle.trust_domain.host_signing_key",
                   return_value="signing-key"):
            tok = _host_auth_token()
        self.assertEqual(ROLE_CLI, verify(tok, "signing-key"))

    def test_returns_empty_when_key_unreadable(self) -> None:
        with patch("bot_bottle.trust_domain.host_signing_key",
                   side_effect=OSError("no host root")):
            self.assertEqual("", _host_auth_token())


class TestBackendProbeFailure(unittest.TestCase):
    def test_records_safe_typed_diagnostic(self) -> None:
        with patch("bot_bottle.control.client.debug") as debug:
            result = _probe_failure("firecracker", RuntimeError("secret detail"))
        self.assertEqual(BackendProbeFailure("firecracker", "RuntimeError"), result)
        rendered = repr(debug.call_args)
        self.assertNotIn("secret detail", rendered)


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
            self.c.register_bottle("10.0.0.9", policy="P", metadata="M")
        sent = json.loads(m.call_args.args[0].data)
        self.assertEqual("10.0.0.9", sent["source_ip"])
        self.assertEqual("P", sent["policy"])
        self.assertNotIn("image_ref", sent)

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


class TestUpdateAgentSecret(unittest.TestCase):
    def setUp(self) -> None:
        self.c = OrchestratorClient("http://orch:8080")

    def test_success_posts_single_secret(self) -> None:
        with patch(_URLOPEN, return_value=_resp(200, {"updated": True})) as opened:
            self.assertTrue(self.c.update_agent_secret("b1", "A", "fresh", "key"))
        request = opened.call_args.args[0]
        self.assertEqual("POST", request.get_method())
        self.assertTrue(request.full_url.endswith("/bottles/b1/secret"))
        self.assertEqual(
            {"name": "A", "value": "fresh", "env_var_secret": "key"},
            json.loads(request.data),
        )

    def test_unknown_bottle_is_false(self) -> None:
        with patch(_URLOPEN, side_effect=_http_error(404)):
            self.assertFalse(self.c.update_agent_secret("b1", "A", "v", "key"))

    def test_other_status_raises(self) -> None:
        with patch(_URLOPEN, side_effect=_http_error(400)):
            with self.assertRaises(OrchestratorClientError):
                self.c.update_agent_secret("b1", "A", "v", "key")


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

    def test_posts_and_returns_reaped(self) -> None:
        """The orchestrator finds the live set itself; the body carries none."""
        with patch(_URLOPEN, return_value=_resp(200, {"reaped": ["b1", "b2"]})) as m:
            got = self.c.reconcile()
        self.assertEqual(["b1", "b2"], got)
        self.assertEqual({}, json.loads(m.call_args.args[0].data))  # server-default grace

    def test_grace_seconds_is_forwarded_when_given(self) -> None:
        with patch(_URLOPEN, return_value=_resp(200, {"reaped": []})) as m:
            self.c.reconcile(grace_seconds=30)
        self.assertEqual({"grace_seconds": 30}, json.loads(m.call_args.args[0].data))

    def test_malformed_reaped_is_tolerated(self) -> None:
        with patch(_URLOPEN, return_value=_resp(200, {"reaped": ["ok", 5, None]})):
            self.assertEqual(["ok"], self.c.reconcile())
        with patch(_URLOPEN, return_value=_resp(200, {})):
            self.assertEqual([], self.c.reconcile())

    def test_error_status_raises(self) -> None:
        # 503: the orchestrator couldn't learn the live set and reaped nothing.
        for code in (500, 503):
            with patch(_URLOPEN, side_effect=_http_error(code)):
                with self.assertRaises(OrchestratorClientError):
                    self.c.reconcile()


class TestSuperviseRespond(unittest.TestCase):
    def setUp(self) -> None:
        self.c = OrchestratorClient("http://orch:8080")

    @staticmethod
    def _conflict(detail: str) -> urllib.error.HTTPError:
        body = io.BytesIO(json.dumps({"detail": detail}).encode())
        return urllib.error.HTTPError("http://x", 409, "conflict", {}, body)  # type: ignore[arg-type]

    def test_success(self) -> None:
        with patch(_URLOPEN, return_value=_resp(200, {"responded": True})):
            self.c.supervise_respond("p1", bottle_slug="demo", decision="approve")

    def test_an_answered_proposal_raises_already_answered(self) -> None:
        with patch(_URLOPEN, side_effect=self._conflict("already answered (rejected)")):
            with self.assertRaises(AlreadyAnswered) as caught:
                self.c.supervise_respond("p1", bottle_slug="demo", decision="approve")
        self.assertEqual("rejected", caught.exception.decided_as)

    def test_another_conflict_is_a_client_error(self) -> None:
        with patch(_URLOPEN, side_effect=self._conflict("no such proposal")):
            with self.assertRaises(OrchestratorClientError) as caught:
                self.c.supervise_respond("p1", bottle_slug="demo", decision="approve")
        self.assertNotIsInstance(caught.exception, AlreadyAnswered)
        self.assertIn("no such proposal", str(caught.exception))


class TestEveryOrchestrator(unittest.TestCase):
    """A host runs one orchestrator per backend with infra up (docker and
    firecracker, say); requests come from all of them."""

    _URLS = ["http://127.0.0.1:8099", "http://10.243.255.1:8099"]

    def _discover(self, healthy: list[str]):
        def health(client: OrchestratorClient) -> bool:
            return client._base in healthy  # pylint: disable=protected-access
        return (patch("bot_bottle.control.client._candidates", return_value=(self._URLS, [])),
                patch.object(OrchestratorClient, "health", health))

    def test_every_healthy_orchestrator_is_found(self) -> None:
        a, b = self._discover(self._URLS)
        with a, b:
            self.assertEqual(self._URLS, discover_orchestrator_urls(MagicMock()))
        a, b = self._discover([])
        with a, b, self.assertRaises(OrchestratorClientError):
            discover_orchestrator_urls(MagicMock())

    def test_pending_is_gathered_and_tagged_and_a_refusing_one_skipped(self) -> None:
        def pending(client: OrchestratorClient) -> list[dict[str, object]]:
            if client._base == self._URLS[0]:  # pylint: disable=protected-access
                raise OrchestratorClientError("HTTP 401 control-plane authentication required")
            return [{"id": "p1", "bottle_slug": "b"}]

        a, b = self._discover(self._URLS)
        with a, b, patch.object(OrchestratorClient, "supervise_pending", pending):
            got = pending_everywhere(MagicMock())
        self.assertEqual([{"id": "p1", "bottle_slug": "b", ORCHESTRATOR_KEY: self._URLS[1]}], got)

    def test_pending_raises_when_none_can_be_asked(self) -> None:
        def refuse(_client: OrchestratorClient) -> list[dict[str, object]]:
            raise OrchestratorClientError("HTTP 401")

        a, b = self._discover(self._URLS)
        with a, b, patch.object(OrchestratorClient, "supervise_pending", refuse), \
                self.assertRaises(OrchestratorClientError):
            pending_everywhere(MagicMock())


class TestDiscoverFirst(unittest.TestCase):
    def test_the_first_healthy_candidate_wins_and_none_raises(self) -> None:
        def on_8099(client: OrchestratorClient) -> bool:
            return client._base.endswith(":8099")  # pylint: disable=protected-access

        def down(_client: OrchestratorClient) -> bool:
            return False

        with patch.object(OrchestratorClient, "health", on_8099):
            self.assertTrue(discover_orchestrator_url(MagicMock()).endswith(":8099"))
        with patch.object(OrchestratorClient, "health", down), \
                self.assertRaises(OrchestratorClientError) as caught:
            discover_orchestrator_url(MagicMock())
        self.assertIn("no running orchestrator", str(caught.exception))

    def test_the_host_controller_wrappers_use_every_orchestrator(self) -> None:
        # pylint: disable=import-outside-toplevel
        from bot_bottle.control.hostctl import remote, requests

        with patch("bot_bottle.control.client.pending_everywhere", return_value=[{"id": "p"}]):
            self.assertEqual([{"id": "p"}], requests.orchestrator_pending())
        self.assertEqual("http://fc:8099", remote._orchestrator("http://fc:8099")._base)  # pylint: disable=protected-access
        with patch("bot_bottle.control.client.discover_orchestrator_url",
                   return_value="http://first:8099"):
            self.assertEqual("http://first:8099", remote._orchestrator()._base)  # pylint: disable=protected-access
