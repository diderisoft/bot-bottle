"""Unit: consolidated launch sequence — compose the orchestrator primitives (PRD 0070)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from bot_bottle.backend.docker.consolidated_launch import (
    launch_consolidated,
    teardown_consolidated,
)
from bot_bottle.egress import EgressPlan, EgressRoute
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.orchestrator.client import RegisteredBottle

_MOD = "bot_bottle.backend.docker.consolidated_launch"


def _egress_plan() -> EgressPlan:
    return EgressPlan(
        slug="demo", routes_path=Path("/x"), routes=(EgressRoute(host="api.example.com"),),
        token_env_map={},
    )


def _git_plan() -> GitGatePlan:
    return GitGatePlan(
        slug="demo", entrypoint_script=Path(), hook_script=Path(),
        access_hook_script=Path(), upstreams=(),
    )


def _client(*, bottles: list[dict[str, object]] | None = None) -> Mock:
    c = Mock()
    c.list_bottles.return_value = bottles or []
    c.register_bottle.return_value = RegisteredBottle("b1", "tok")
    return c


class TestLaunchConsolidated(unittest.TestCase):
    def _run(self, client: Mock, provision: Mock | None = None):
        process = MagicMock()
        process.ensure_running.return_value = "http://orch:8080"
        with patch(f"{_MOD}._network_cidr", return_value="172.18.0.0/16"), \
                patch(f"{_MOD}._container_ip", return_value="172.18.0.2"), \
                patch(f"{_MOD}.OrchestratorClient", return_value=client), \
                patch(f"{_MOD}.provision_git_gate", provision or Mock()):
            return launch_consolidated(_egress_plan(), _git_plan(), process=process)

    def test_allocates_ip_registers_and_provisions(self) -> None:
        client = _client()
        provision = Mock()
        ctx = self._run(client, provision)
        # .1 is the router, .2 is the gateway → first bottle gets .3.
        self.assertEqual("172.18.0.3", ctx.source_ip)
        self.assertEqual("172.18.0.2", ctx.gateway_ip)
        self.assertEqual("b1", ctx.bottle_id)
        self.assertEqual("tok", ctx.identity_token)
        self.assertEqual("http://orch:8080", ctx.orchestrator_url)
        # Registered with the source IP + the egress policy blob.
        kwargs = client.register_bottle.call_args
        self.assertEqual("172.18.0.3", kwargs.args[0])
        self.assertIn("api.example.com", kwargs.kwargs["policy"])
        provision.assert_called_once()

    def test_skips_gateway_and_live_bottle_addresses(self) -> None:
        client = _client(bottles=[{"source_ip": "172.18.0.3"}])
        ctx = self._run(client)
        self.assertEqual("172.18.0.4", ctx.source_ip)  # .2 gw, .3 taken → .4

    def test_provision_failure_rolls_back_registration(self) -> None:
        client = _client()
        provision = Mock(side_effect=RuntimeError("provision boom"))
        with self.assertRaises(RuntimeError):
            self._run(client, provision)
        client.teardown_bottle.assert_called_once_with("b1")  # no orphan left


class TestTeardownConsolidated(unittest.TestCase):
    def test_deregisters_and_deprovisions(self) -> None:
        client = Mock()
        with patch(f"{_MOD}.OrchestratorClient", return_value=client), \
                patch(f"{_MOD}.deprovision_git_gate") as deprov:
            teardown_consolidated("b1", orchestrator_url="http://orch:8080")
        client.teardown_bottle.assert_called_once_with("b1")
        deprov.assert_called_once()


if __name__ == "__main__":
    unittest.main()
