"""Unit: consolidated launch sequence — compose the orchestrator primitives (PRD 0070)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from bot_bottle.backend.docker.consolidated_launch import (
    ConsolidatedLaunchError,
    network_container_ips,
    launch_consolidated,
    deprovision_consolidated,
)
from bot_bottle.egress import EgressPlan, EgressRoute
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.control.client import RegisteredBottle

_MOD = "bot_bottle.backend.docker.consolidated_launch"
_UTIL = "bot_bottle.backend.provision_bottle"


def _egress_plan() -> EgressPlan:
    return EgressPlan(
        slug="demo", routes_path=Path("/x"), routes=(EgressRoute(host="api.example.com"),),
        token_env_map={},
    )


def _git_plan() -> GitGatePlan:
    return GitGatePlan(
        slug="demo", hook_script=Path(),
        access_hook_script=Path(), upstreams=(),
    )


def _client(*, bottles: list[dict[str, object]] | None = None) -> Mock:
    c = Mock()
    c.list_bottles.return_value = bottles or []
    c.register_bottle.return_value = RegisteredBottle("b1", "tok")
    return c


class TestLaunchConsolidated(unittest.TestCase):
    def setUp(self) -> None:
        self.create = Mock(return_value="10.242.0.8/29")
        self.connect = Mock()
        self.remove = Mock()

    def _run(self, client: Mock, provision: Mock | None = None):
        service = MagicMock()
        service.ensure_running.return_value = "http://orch:8080"
        gateway = service.gateway.return_value
        gateway.name = "bot-bottle-gateway"
        with patch(f"{_MOD}.create_agent_network", self.create), \
                patch(f"{_MOD}.connect_gateway", self.connect), \
                patch(f"{_MOD}.remove_agent_network", self.remove), \
                patch(f"{_MOD}.OrchestratorClient", return_value=client), \
                patch(f"{_UTIL}.provision_git_gate", provision or Mock()):
            return launch_consolidated(_egress_plan(), _git_plan(), service=service,
                                       agent_network="bot-bottle-agent-demo")

    def test_creates_its_network_joins_the_gateway_registers_and_provisions(self) -> None:
        client = _client()
        provision = Mock()
        ctx = self._run(client, provision)
        self.create.assert_called_once_with("bot-bottle-agent-demo")
        # .1 is docker's router, .2 the gateway, .3 the agent.
        self.connect.assert_called_once_with(
            "bot-bottle-agent-demo", "bot-bottle-gateway", "10.242.0.10")
        self.assertEqual("10.242.0.11", ctx.source_ip)
        self.assertEqual("10.242.0.10", ctx.gateway_ip)
        self.assertEqual("bot-bottle-agent-demo", ctx.network)
        self.assertEqual("b1", ctx.bottle_id)
        self.assertEqual("tok", ctx.identity_token)
        # Registered with the source IP + the egress policy blob.
        kwargs = client.register_bottle.call_args
        self.assertEqual("10.242.0.11", kwargs.args[0])
        self.assertIn("api.example.com", kwargs.kwargs["policy"])
        provision.assert_called_once()
        self.remove.assert_not_called()

    def test_other_bottles_are_its_peers(self) -> None:
        client = _client(bottles=[
            {"source_ip": "10.242.0.3"}, {"source_ip": "10.242.0.11"},
            {"source_ip": "10.242.0.19"}, {"source_ip": None}, {},
        ])
        ctx = self._run(client)
        self.assertEqual(("10.242.0.19", "10.242.0.3"), ctx.peer_ips)

    def test_provision_failure_rolls_back_registration_and_network(self) -> None:
        client = _client()
        provision = Mock(side_effect=RuntimeError("provision boom"))
        with self.assertRaises(RuntimeError):
            self._run(client, provision)
        client.teardown_bottle.assert_called_once_with("b1")  # no orphan left
        self.remove.assert_called_once_with("bot-bottle-agent-demo", "bot-bottle-gateway")


class TestNetworkContainerIps(unittest.TestCase):
    def test_fails_closed_when_network_inspection_fails(self) -> None:
        result = Mock(returncode=1, stdout="", stderr="daemon unavailable")
        with (
            patch(f"{_MOD}.run_docker", return_value=result),
            self.assertRaisesRegex(ConsolidatedLaunchError, "daemon unavailable"),
        ):
            network_container_ips("bot-bottle-gateway")


class TestTeardownConsolidated(unittest.TestCase):
    def test_deregisters_and_deprovisions(self) -> None:
        client = Mock()
        service = MagicMock()
        service.orchestrator.return_value.url.return_value = "http://orch:8080"
        with patch(f"{_UTIL}.OrchestratorClient", return_value=client) as make, \
                patch(f"{_UTIL}.deprovision_git_gate") as deprov:
            deprovision_consolidated("b1", service=service)
        # Resolved at teardown: the orchestrator may have moved since launch.
        self.assertEqual("http://orch:8080", make.call_args.args[0])
        client.teardown_bottle.assert_called_once_with("b1")
        deprov.assert_called_once()

    def test_a_stopped_orchestrator_is_a_clear_error(self) -> None:
        from bot_bottle.control.client import OrchestratorClientError
        service = MagicMock()
        service.orchestrator.return_value.url.return_value = ""
        with patch(f"{_UTIL}.deprovision_git_gate"), \
                self.assertRaisesRegex(OrchestratorClientError, "isn't running"):
            deprovision_consolidated("b1", service=service)


if __name__ == "__main__":
    unittest.main()
