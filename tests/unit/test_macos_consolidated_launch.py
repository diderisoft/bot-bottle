"""Unit: macOS consolidated launch — register-after-start (PRD 0070)."""

from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from bot_bottle.backend.macos_container.consolidated_launch import (
    GatewayEndpoint,
    ensure_gateway,
    register_agent,
    deprovision_consolidated,
)
from bot_bottle.egress import EgressPlan, EgressRoute
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.control.client import OrchestratorClientError, RegisteredBottle

_MOD = "bot_bottle.backend.macos_container.consolidated_launch"
_UTIL = "bot_bottle.backend.provision_bottle"


def _egress_plan() -> EgressPlan:
    return EgressPlan(
        slug="demo", routes_path=Path("/x"),
        routes=(EgressRoute(host="api.example.com"),), token_env_map={},
    )


def _git_plan() -> GitGatePlan:
    return GitGatePlan(
        slug="demo", hook_script=Path(),
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
    c.list_bottles.return_value = [{"source_ip": "192.168.128.9"},
                                   {"source_ip": "192.168.128.20"}]
    return c


class TestEnsureGateway(unittest.TestCase):
    def _run(self, service: MagicMock) -> GatewayEndpoint:
        return ensure_gateway(service=service)

    def _service(self) -> MagicMock:
        service = MagicMock()
        # Two containers now: the orchestrator on the control network, the
        # gateway on the agent network — distinct addresses. `ensure_running`
        # returns the control-plane URL; the gateway address comes off the
        # gateway service.
        service.ensure_running.return_value = "http://192.168.128.2:8099"
        service.gateway.return_value.address.return_value = "192.168.128.3"
        service.network = "bot-bottle-mac-gateway"
        service.ca_cert_pem.return_value = "PEM"
        return service

    def test_reports_gateway_endpoint(self) -> None:
        endpoint = self._run(self._service())
        self.assertEqual("http://192.168.128.2:8099", endpoint.orchestrator_url)
        self.assertEqual("192.168.128.3", endpoint.gateway_ip)
        self.assertEqual("PEM", endpoint.gateway_ca_pem)
        self.assertEqual("bot-bottle-mac-gateway", endpoint.network)

    def test_gateway_ip_is_distinct_from_the_control_plane(self) -> None:
        # The planes are separate containers, so the agent's proxy target (the
        # gateway) is a different address from the control-plane host.
        endpoint = self._run(self._service())
        orch_host = endpoint.orchestrator_url.split("://")[1].split(":")[0]
        self.assertNotEqual(orch_host, endpoint.gateway_ip)


class TestRegisterAgent(unittest.TestCase):
    def _run(
        self, client: Mock, provision: Mock | None = None,
        *, source_ip: str = "192.168.128.9",
    ):
        with patch(f"{_MOD}.OrchestratorClient", return_value=client), \
                patch(f"{_UTIL}.provision_git_gate", provision or Mock()):
            return register_agent(
                _egress_plan(), _git_plan(),
                source_ip=source_ip, endpoint=_endpoint(),
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

    def test_the_probe_knocks_on_every_other_bottle(self) -> None:
        self.assertEqual(("192.168.128.20",), self._run(_client()).peer_ips)

    def test_siblings_that_cannot_be_listed_are_not_knocked_on(self) -> None:
        client = _client()
        client.list_bottles.side_effect = OrchestratorClientError("unreachable")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual((), self._run(client).peer_ips)

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
        service = MagicMock()
        service.orchestrator.return_value.url.return_value = "http://o:8099"
        with patch(f"{_UTIL}.OrchestratorClient", return_value=client) as make, \
                patch(f"{_UTIL}.deprovision_git_gate", deprovision):
            deprovision_consolidated("b1", service=service)
        # The orchestrator's DHCP address moves on restart: resolved now.
        self.assertEqual("http://o:8099", make.call_args.args[0])
        client.teardown_bottle.assert_called_once_with("b1")
        self.assertEqual("b1", deprovision.call_args.args[1])


if __name__ == "__main__":
    unittest.main()


class TestRegisterAgentReconciles(unittest.TestCase):
    """Registration self-heals the registry first: an orphan row at a recycled
    address makes attribution ambiguous, which resolves no policy at all and
    denies every host for the bottle being launched."""

    def _register(self, client: Mock) -> None:
        with patch(f"{_MOD}.OrchestratorClient", return_value=client), \
                patch(f"{_UTIL}.provision_git_gate"):
            register_agent(
                _egress_plan(), _git_plan(),
                source_ip="10.0.0.7", endpoint=_endpoint(),
            )

    def test_reconciles_before_registering(self) -> None:
        client = _client()
        calls: list[str] = []

        def _reconcile(*_args: object, **_kwargs: object) -> list[str]:
            calls.append("reconcile")
            return []

        def _register_bottle(*_args: object, **_kwargs: object) -> RegisteredBottle:
            calls.append("register")
            return RegisteredBottle("b1", "tok")

        client.reconcile.side_effect = _reconcile
        client.register_bottle.side_effect = _register_bottle
        self._register(client)
        self.assertEqual(["reconcile", "register"], calls)
        # The orchestrator asks the host controller for the live set.
        client.reconcile.assert_called_once_with()

    def test_a_reconcile_failure_does_not_block_the_launch(self) -> None:
        client = _client()
        client.reconcile.side_effect = OrchestratorClientError("unreachable")
        self._register(client)
        client.register_bottle.assert_called_once()
