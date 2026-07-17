"""Unit: macOS consolidated launch — register-after-start (PRD 0070)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from bot_bottle.backend.macos_container.consolidated_launch import (
    GatewayEndpoint,
    ensure_gateway,
    register_agent,
    teardown_consolidated,
)
from bot_bottle.egress import EgressPlan, EgressRoute
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.orchestrator.client import RegisteredBottle

_MOD = "bot_bottle.backend.macos_container.consolidated_launch"


def _egress_plan() -> EgressPlan:
    return EgressPlan(
        slug="demo", routes_path=Path("/x"),
        routes=(EgressRoute(host="api.example.com"),), token_env_map={},
    )


def _git_plan() -> GitGatePlan:
    return GitGatePlan(
        slug="demo", entrypoint_script=Path(), hook_script=Path(),
        access_hook_script=Path(), upstreams=(),
    )


def _endpoint() -> GatewayEndpoint:
    return GatewayEndpoint(
        orchestrator_url="http://192.168.128.2:8099",
        gateway_ip="192.168.128.3",
        gateway_ca_pem="-----BEGIN CERTIFICATE-----\n",
        network="bot-bottle-mac-gateway",
    )


def _client() -> Mock:
    c = Mock()
    c.register_bottle.return_value = RegisteredBottle("b1", "tok")
    return c


class TestEnsureGateway(unittest.TestCase):
    def _run(self, service: MagicMock) -> GatewayEndpoint:
        with patch(f"{_MOD}.MacosInfraService", return_value=service):
            return ensure_gateway()

    def _service(self) -> MagicMock:
        from bot_bottle.backend.macos_container.infra import InfraEndpoint
        service = MagicMock()
        service.ensure_running.return_value = InfraEndpoint(
            control_plane_url="http://192.168.128.2:8099",
            gateway_ip="192.168.128.2",
        )
        service.network = "bot-bottle-mac-gateway"
        service.ca_cert_pem.return_value = "PEM"
        return service

    def test_reports_gateway_endpoint(self) -> None:
        endpoint = self._run(self._service())
        self.assertEqual("http://192.168.128.2:8099", endpoint.orchestrator_url)
        self.assertEqual("192.168.128.2", endpoint.gateway_ip)
        self.assertEqual("PEM", endpoint.gateway_ca_pem)
        self.assertEqual("bot-bottle-mac-gateway", endpoint.network)

    def test_control_plane_and_gateway_share_one_address(self) -> None:
        """One infra container hosts both, so the gateway IP and the
        control-plane host are the same."""
        endpoint = self._run(self._service())
        self.assertEqual(
            endpoint.gateway_ip,
            endpoint.orchestrator_url.split("://")[1].split(":")[0],
        )


class TestRegisterAgent(unittest.TestCase):
    def _run(
        self, client: Mock, provision: Mock | None = None,
        *, source_ip: str = "192.168.128.9",
    ):
        with patch(f"{_MOD}.OrchestratorClient", return_value=client), \
                patch(f"{_MOD}.provision_git_gate", provision or Mock()):
            return register_agent(
                _egress_plan(), _git_plan(),
                source_ip=source_ip, endpoint=_endpoint(), image_ref="img:1",
            )

    def test_registers_by_the_address_read_from_the_live_container(self) -> None:
        """The attribution key is the DHCP-assigned address the caller read
        back — there is no --ip to pin it up front."""
        client = _client()
        ctx = self._run(client, source_ip="192.168.128.9")
        self.assertEqual("192.168.128.9", ctx.source_ip)
        self.assertEqual("192.168.128.9", client.register_bottle.call_args.args[0])

    def test_returns_identity_token_and_bottle_id(self) -> None:
        ctx = self._run(_client())
        self.assertEqual("b1", ctx.bottle_id)
        self.assertEqual("tok", ctx.identity_token)

    def test_provisions_git_gate_for_the_registered_bottle(self) -> None:
        provision = Mock()
        self._run(_client(), provision)
        self.assertEqual("b1", provision.call_args.args[1])

    def test_rolls_registration_back_when_provisioning_fails(self) -> None:
        """A provisioning failure must not leave an orphan registration
        holding the source IP."""
        client = _client()
        provision = Mock(side_effect=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            self._run(client, provision)
        client.teardown_bottle.assert_called_once_with("b1")


class TestTeardown(unittest.TestCase):
    def test_deregisters_and_deprovisions(self) -> None:
        client = Mock()
        deprovision = Mock()
        with patch(f"{_MOD}.OrchestratorClient", return_value=client), \
                patch(f"{_MOD}.deprovision_git_gate", deprovision):
            teardown_consolidated("b1", orchestrator_url="http://o:8099")
        client.teardown_bottle.assert_called_once_with("b1")
        self.assertEqual("b1", deprovision.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
