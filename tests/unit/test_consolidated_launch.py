"""Unit: consolidated launch sequence — compose the orchestrator primitives (PRD 0070)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from bot_bottle.backend.docker.consolidated_launch import (
    ConsolidatedLaunchError,
    _network_container_ips,
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
        slug="demo", entrypoint_script=Path(), hook_script=Path(),
        access_hook_script=Path(), upstreams=(),
    )


def _client(*, bottles: list[dict[str, object]] | None = None) -> Mock:
    c = Mock()
    c.list_bottles.return_value = bottles or []
    c.register_bottle.return_value = RegisteredBottle("b1", "tok")
    return c


class TestLaunchConsolidated(unittest.TestCase):
    def _run(
        self, client: Mock, provision: Mock | None = None,
        *, on_network: tuple[str, ...] = ("172.18.0.2",),
    ):
        service = MagicMock()
        service.ensure_running.return_value = "http://orch:8080"
        # The launch flow reads the gateway's agent-facing address + provisioning
        # transport off the Gateway service (service.gateway()).
        gateway = service.gateway.return_value
        gateway.name = "bot-bottle-gateway"
        gateway.address.return_value = "172.18.0.2"
        with patch(f"{_MOD}._network_cidr", return_value="172.18.0.0/16"), \
                patch(f"{_MOD}._network_container_ips", return_value=list(on_network)), \
                patch(f"{_MOD}.OrchestratorClient", return_value=client), \
                patch(f"{_UTIL}.provision_git_gate", provision or Mock()):
            return launch_consolidated(_egress_plan(), _git_plan(), service=service)

    def test_allocates_ip_registers_and_provisions(self) -> None:
        client = _client()
        provision = Mock()
        ctx = self._run(client, provision)
        # .1 is the router, .2 is the gateway → first bottle gets .3.
        self.assertEqual("172.18.0.3", ctx.source_ip)
        self.assertEqual("172.18.0.2", ctx.gateway_ip)
        self.assertEqual("b1", ctx.bottle_id)
        self.assertEqual("tok", ctx.identity_token)
        # Registered with the source IP + the egress policy blob.
        kwargs = client.register_bottle.call_args
        self.assertEqual("172.18.0.3", kwargs.args[0])
        self.assertIn("api.example.com", kwargs.kwargs["policy"])
        provision.assert_called_once()

    def test_skips_all_addresses_on_the_network(self) -> None:
        # Gateway .2 + orchestrator .3 already attached -> agent gets .4.
        ctx = self._run(_client(), on_network=("172.18.0.2", "172.18.0.3"))
        self.assertEqual("172.18.0.4", ctx.source_ip)

    def test_provision_failure_rolls_back_registration(self) -> None:
        client = _client()
        provision = Mock(side_effect=RuntimeError("provision boom"))
        with self.assertRaises(RuntimeError):
            self._run(client, provision)
        client.teardown_bottle.assert_called_once_with("b1")  # no orphan left


class TestNetworkContainerIps(unittest.TestCase):
    def test_fails_closed_when_network_inspection_fails(self) -> None:
        result = Mock(returncode=1, stdout="", stderr="daemon unavailable")
        with (
            patch(f"{_MOD}.run_docker", return_value=result),
            self.assertRaisesRegex(ConsolidatedLaunchError, "daemon unavailable"),
        ):
            _network_container_ips("bot-bottle-gateway")


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
