"""Unit: macOS consolidated launch — register-after-start (PRD 0070)."""

from __future__ import annotations

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
from bot_bottle.orchestrator.client import RegisteredBottle

_MOD = "bot_bottle.backend.macos_container.consolidated_launch"
_UTIL = "bot_bottle.backend.provision_bottle"


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
        # Two containers now: the orchestrator on the control network, the
        # gateway on the agent network — distinct addresses.
        service.ensure_running.return_value = InfraEndpoint(
            orchestrator_url="http://192.168.128.2:8099",
            gateway_ip="192.168.128.3",
        )
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
                patch(f"{_UTIL}.provision_git_gate", provision or Mock()), \
                patch(f"{_MOD}.live_source_ips", return_value=[]):
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
        with patch(f"{_UTIL}.OrchestratorClient", return_value=client), \
                patch(f"{_UTIL}.deprovision_git_gate", deprovision):
            deprovision_consolidated("b1", orchestrator_url="http://o:8099")
        client.teardown_bottle.assert_called_once_with("b1")
        self.assertEqual("b1", deprovision.call_args.args[1])


if __name__ == "__main__":
    unittest.main()


class TestLiveSourceIps(unittest.TestCase):
    """The reconciliation input: the host enumerates its own bottles because
    the orchestrator, inside the infra container, cannot see the backend."""

    def _agents(self, *slugs: str) -> list[Mock]:
        return [Mock(slug=s) for s in slugs]

    def test_maps_slugs_to_container_addresses(self) -> None:
        from bot_bottle.backend.macos_container.consolidated_launch import live_source_ips
        with patch(f"{_MOD}.enumerate_active", return_value=self._agents("a", "b")), \
                patch(f"{_MOD}.container_mod.inspect_container_network_ip",
                      side_effect=["10.0.0.1", "10.0.0.2"]) as ip:
            got = live_source_ips("net0")
        self.assertEqual(["10.0.0.1", "10.0.0.2"], got)
        self.assertEqual("bot-bottle-a", ip.call_args_list[0].args[0])

    def test_containers_without_an_address_are_skipped(self) -> None:
        """A container that hasn't been given a DHCP address yet contributes
        nothing — the reap's grace window, not this list, protects it."""
        from bot_bottle.backend.macos_container.consolidated_launch import live_source_ips
        with patch(f"{_MOD}.enumerate_active", return_value=self._agents("a", "b")), \
                patch(f"{_MOD}.container_mod.inspect_container_network_ip",
                      side_effect=["", "10.0.0.2"]):
            self.assertEqual(["10.0.0.2"], live_source_ips("net0"))

    def test_container_list_failure_raises(self) -> None:
        """If container list fails, the live set is not authoritative and
        reconciliation must be skipped."""
        from bot_bottle.backend.macos_container.consolidated_launch import live_source_ips
        from bot_bottle.backend.macos_container.enumerate import EnumerationError
        with patch(f"{_MOD}.enumerate_active",
                   side_effect=EnumerationError("container list failed")):
            with self.assertRaises(EnumerationError):
                live_source_ips("net0")

    def test_per_container_inspect_failure_raises(self) -> None:
        """If any individual inspect fails, the live set is not authoritative."""
        from bot_bottle.backend.macos_container.consolidated_launch import live_source_ips
        from bot_bottle.backend.macos_container.enumerate import EnumerationError
        with patch(f"{_MOD}.enumerate_active", return_value=self._agents("a", "b")), \
                patch(f"{_MOD}.container_mod.inspect_container_network_ip",
                      side_effect=["10.0.0.1", None]):
            with self.assertRaises(EnumerationError):
                live_source_ips("net0")


class TestRegisterAgentReconciles(unittest.TestCase):
    """Registration self-heals the registry first: an orphan row at a recycled
    address makes attribution ambiguous, which resolves no policy at all and
    denies every host for the bottle being launched."""

    def _register(self, client: Mock) -> None:
        with patch(f"{_MOD}.OrchestratorClient", return_value=client), \
                patch(f"{_UTIL}.provision_git_gate"), \
                patch(f"{_MOD}.live_source_ips", return_value=["10.0.0.7"]):
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
        client.reconcile.assert_called_once_with(["10.0.0.7"])

    def test_a_reconcile_failure_does_not_block_the_launch(self) -> None:
        from bot_bottle.orchestrator.client import OrchestratorClientError
        client = _client()
        client.reconcile.side_effect = OrchestratorClientError("unreachable")
        self._register(client)
        client.register_bottle.assert_called_once()

    def test_enumeration_error_does_not_block_the_launch(self) -> None:
        """A partial container listing must not abort the launch — skip
        reconciliation and proceed, just as with an unreachable orchestrator."""
        from bot_bottle.backend.macos_container.enumerate import EnumerationError
        client = _client()
        with patch(f"{_MOD}.OrchestratorClient", return_value=client), \
                patch(f"{_UTIL}.provision_git_gate"), \
                patch(f"{_MOD}.live_source_ips",
                      side_effect=EnumerationError("container list failed")):
            register_agent(
                _egress_plan(), _git_plan(),
                source_ip="10.0.0.7", endpoint=_endpoint(),
            )
        client.register_bottle.assert_called_once()
