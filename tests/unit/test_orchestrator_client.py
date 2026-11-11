"""Unit: host-side orchestrator control-plane client (PRD 0070). HTTP mocked."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle import infra_state
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
    NoOrchestrator,
    OrchestratorEndpoint,
    env_orchestrator_url,
    first_accepting,
    listening_orchestrators,
    pending_proposals,
    record_orchestrator,
    recorded_orchestrator,
    stray_orchestrators,
)

from bot_bottle.control.hostctl import runner
from tests.unit import as_cli, serve_host_controller

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


_FC = "http://10.243.255.1:8099"
_STRAY = "http://127.0.0.1:8099"


class _Root(unittest.TestCase):
    """A throwaway state root for the environment's orchestrator record."""

    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())  # pylint: disable=consider-using-with
        self.root = Path(tmp)
        self.enterContext(patch.dict(os.environ, {"BOT_BOTTLE_ROOT": tmp}))
        os.environ.pop("BOT_BOTTLE_ENV", None)

    def route(self, answers: dict[str, object]):  # type: ignore[no-untyped-def]
        """Patch urlopen so each orchestrator URL answers as `answers` says: a
        status code, or an exception (unreachable). Records the URLs asked."""
        asked: list[str] = []

        def urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
            del timeout
            url = request.full_url
            base = next(b for b in answers if url.startswith(b))
            asked.append(base)
            answer = answers[base]
            if isinstance(answer, BaseException):
                raise answer
            assert isinstance(answer, int)
            if answer >= 400:
                raise _http_error(answer)
            return _resp(answer, {})

        return patch(_URLOPEN, side_effect=urlopen), asked


class TestEnvOrchestrator(_Root):
    def test_record_round_trips_in_the_host_db(self) -> None:
        self.assertIsNone(recorded_orchestrator())
        record_orchestrator("firecracker", _FC)
        self.assertEqual(OrchestratorEndpoint("firecracker", _FC), recorded_orchestrator())
        self.assertEqual({"orchestrator.backend": "firecracker", "orchestrator.url": _FC},
                         infra_state.recorded())
        self.assertFalse((self.root / "infra" / "orchestrator.json").exists())

    def test_outside_the_host_controller_it_is_read_from_a_running_one(self) -> None:
        record_orchestrator("firecracker", _FC)
        as_cli(self)
        with patch("bot_bottle.control.hostctl.runner.running", return_value=None):
            self.assertIsNone(recorded_orchestrator())  # none is started for it
        with self.assertRaisesRegex(RuntimeError, "only the host controller"):
            record_orchestrator("docker", "http://127.0.0.1:1")
        serve_host_controller(self)
        state = runner.ensure_running()
        with patch("bot_bottle.control.hostctl.runner.running", return_value=state):
            self.assertEqual(OrchestratorEndpoint("firecracker", _FC), recorded_orchestrator())

    def test_none_recorded_says_so_and_never_probes_other_backends(self) -> None:
        with patch("bot_bottle.control.client._candidates",
                   side_effect=AssertionError("probed")), \
                self.assertRaises(NoOrchestrator) as caught:
            env_orchestrator_url()
        self.assertEqual("no orchestrator for environment live; run `bot-bottle up`",
                         str(caught.exception))

    def test_the_recorded_one_is_used_and_a_down_one_named(self) -> None:
        record_orchestrator("firecracker", _FC)
        opened, asked = self.route({_FC: 200, _STRAY: 200})
        with opened, patch("bot_bottle.control.client._candidates",
                           side_effect=AssertionError("probed")):
            self.assertEqual(_FC, env_orchestrator_url())
        self.assertEqual([_FC], asked)
        opened, _ = self.route({_FC: urllib.error.URLError("refused")})
        with opened, self.assertRaises(NoOrchestrator) as caught:
            env_orchestrator_url()
        self.assertIn(f"environment live (firecracker at {_FC}) isn't answering",
                      str(caught.exception))
        self.assertIn("run `bot-bottle up`", str(caught.exception))

    def test_pending_proposals_come_from_the_environments_and_are_tagged(self) -> None:
        record_orchestrator("firecracker", _FC)
        with patch.object(OrchestratorClient, "health", return_value=True), \
                patch.object(OrchestratorClient, "supervise_pending",
                             return_value=[{"id": "p1", "bottle_slug": "b"}]):
            got = pending_proposals()
        self.assertEqual([{"id": "p1", "bottle_slug": "b", ORCHESTRATOR_KEY: _FC}], got)

    def test_the_host_controller_wrappers_use_the_environments(self) -> None:
        # pylint: disable=import-outside-toplevel
        from bot_bottle.control.hostctl import remote, requests

        with patch("bot_bottle.control.client.pending_proposals", return_value=[{"id": "p"}]):
            self.assertEqual([{"id": "p"}], requests.orchestrator_pending())
        self.assertEqual("http://fc:8099", remote._orchestrator("http://fc:8099")._base)  # pylint: disable=protected-access
        with patch("bot_bottle.control.client.env_orchestrator_url", return_value=_FC):
            self.assertEqual(_FC, remote._orchestrator()._base)  # pylint: disable=protected-access


class TestCredentialPush(_Root):
    """The 2026-10-31 bug: a stray live-named docker orchestrator (another
    key, so 401) listed first stopped every credential refresh."""

    def _push(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.control.hostctl.bottles import _push_credential
        _push_credential("b1", "EGRESS_TOKEN_0", "fresh", "key")

    def test_goes_to_the_recorded_orchestrator_only(self) -> None:
        record_orchestrator("firecracker", _FC)
        opened, asked = self.route({_STRAY: 401, _FC: 200})
        with opened, patch("bot_bottle.control.client._candidates",
                           side_effect=AssertionError("probed")):
            self._push()
        self.assertEqual([_FC], asked)

    def test_the_recorded_one_not_knowing_the_bottle_or_down_is_named(self) -> None:
        record_orchestrator("firecracker", _FC)
        opened, _ = self.route({_FC: 404})
        with opened, self.assertRaises(OrchestratorClientError) as caught:
            self._push()
        self.assertIn(f"(firecracker at {_FC}) doesn't know bottle b1", str(caught.exception))
        opened, _ = self.route({_FC: 401})
        with opened, self.assertRaises(OrchestratorClientError) as caught:
            self._push()
        self.assertIn("rejected this host's key (HTTP 401)", str(caught.exception))
        opened, _ = self.route({_FC: urllib.error.URLError("no route")})
        with opened, self.assertRaises(NoOrchestrator) as caught:
            self._push()
        self.assertIn("is unreachable", str(caught.exception))

    def test_unrecorded_falls_back_to_listening_ones_skipping_rejections(self) -> None:
        other = "http://192.168.128.2:8099"
        listening = [OrchestratorEndpoint("docker", _STRAY),
                     OrchestratorEndpoint("macos-container", other),
                     OrchestratorEndpoint("firecracker", _FC)]
        opened, asked = self.route({_STRAY: 401, other: 404, _FC: 200})
        with opened, patch("bot_bottle.control.client.listening_orchestrators",
                           return_value=listening):
            self._push()
        self.assertEqual([_STRAY, other, _FC], asked)

    def test_unrecorded_fails_only_when_none_accepted_listing_each(self) -> None:
        other = "http://192.168.128.2:8099"
        listening = [OrchestratorEndpoint("docker", _STRAY),
                     OrchestratorEndpoint("macos-container", other),
                     OrchestratorEndpoint("firecracker", _FC)]
        opened, _ = self.route({_STRAY: 403, other: 404,
                                _FC: urllib.error.URLError("timed out")})
        with opened, patch("bot_bottle.control.client.listening_orchestrators",
                           return_value=listening), \
                self.assertRaises(OrchestratorClientError) as caught:
            self._push()
        message = str(caught.exception)
        self.assertIn("no orchestrator accepted bottle b1", message)
        self.assertIn(f"{_STRAY}: rejected this host's key (HTTP 403)", message)
        self.assertIn(f"{other}: doesn't know bottle b1 (HTTP 404)", message)
        self.assertIn(f"{_FC}: unreachable", message)
        with patch("bot_bottle.control.client.listening_orchestrators", return_value=[]), \
                self.assertRaises(NoOrchestrator):
            self._push()


class TestFirstAccepting(unittest.TestCase):
    def test_stops_at_the_first_acceptance(self) -> None:
        calls: list[str] = []

        def call(client: OrchestratorClient) -> bool:
            calls.append(client._base)  # pylint: disable=protected-access
            if client._base == "http://a":  # pylint: disable=protected-access
                raise OrchestratorClientError("HTTP 500", status=500)
            return True

        self.assertEqual("http://b", first_accepting(["http://a", "http://b", "http://c"],
                                                     call, what="x"))
        self.assertEqual(["http://a", "http://b"], calls)


class TestListening(_Root):
    def test_strays_are_the_listening_ones_that_arent_recorded(self) -> None:
        candidates = [OrchestratorEndpoint("docker", _STRAY),
                      OrchestratorEndpoint("firecracker", _FC)]
        with patch("bot_bottle.control.client._candidates", return_value=(candidates, [])), \
                patch.object(OrchestratorClient, "health", return_value=True):
            self.assertEqual(candidates, listening_orchestrators(MagicMock()))
            self.assertEqual(candidates, stray_orchestrators(MagicMock()))
            record_orchestrator("firecracker", _FC)
            self.assertEqual([candidates[0]], stray_orchestrators(MagicMock()))

    def test_firecracker_is_only_probed_on_linux(self) -> None:
        # pylint: disable=import-outside-toplevel
        from bot_bottle.control.client import _candidates

        settings = MagicMock()
        with patch("bot_bottle.backend.firecracker.util.is_linux", return_value=False), \
                patch("bot_bottle.backend.firecracker.netpool.from_settings") as pool:
            _candidates(settings)
        pool.assert_not_called()
        with patch("bot_bottle.backend.firecracker.util.is_linux", return_value=True), \
                patch("bot_bottle.backend.firecracker.netpool.from_settings") as pool:
            pool.return_value.orch_slot.return_value.guest_ip = "10.9.9.1"
            candidates, _ = _candidates(settings)
        self.assertIn(OrchestratorEndpoint("firecracker", "http://10.9.9.1:8099"), candidates)
