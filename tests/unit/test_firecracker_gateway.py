"""Unit: the Firecracker gateway data plane as a microVM (PRD 0070)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.backend.firecracker import infra_vm
from bot_bottle.backend.firecracker.gateway import (
    GATEWAY_NAME,
    FirecrackerGateway,
)
from bot_bottle.gateway import GatewayError

_GW = "bot_bottle.backend.firecracker.gateway"

_ORCH_URL = "http://10.243.255.1:8099"
_TOKEN = "gw-jwt-token"


class TestFirecrackerGatewayConnect(unittest.TestCase):
    def test_default_name(self) -> None:
        self.assertEqual(GATEWAY_NAME, FirecrackerGateway().name)

    def test_refuses_without_orchestrator_url(self) -> None:
        gw = FirecrackerGateway()
        with patch.object(infra_vm, "boot_gateway") as boot:
            with self.assertRaises(GatewayError):
                gw.connect_to_orchestrator("", _TOKEN)
            boot.assert_not_called()

    def test_refuses_without_gateway_token(self) -> None:
        gw = FirecrackerGateway()
        with patch.object(infra_vm, "boot_gateway") as boot:
            with self.assertRaises(GatewayError):
                gw.connect_to_orchestrator(_ORCH_URL, "")
            boot.assert_not_called()

    def test_refuses_a_url_without_a_host(self) -> None:
        gw = FirecrackerGateway()
        with patch.object(infra_vm, "boot_gateway") as boot:
            with self.assertRaises(GatewayError):
                gw.connect_to_orchestrator("http://:8099", _TOKEN)
            boot.assert_not_called()

    def test_boots_the_gateway_vm_against_the_orchestrator_guest_ip(self) -> None:
        gw = FirecrackerGateway()
        booted = infra_vm.InfraVm(guest_ip="10.243.255.3", private_key=Path("/k"))
        with patch.object(infra_vm, "boot_gateway", return_value=booted) as boot:
            gw.connect_to_orchestrator(_ORCH_URL, _TOKEN)
        # The orchestrator guest IP is parsed off the URL; the token is threaded
        # through unchanged (the gateway never mints — #469).
        boot.assert_called_once_with("10.243.255.1", _TOKEN)


class TestFirecrackerGatewaySurface(unittest.TestCase):
    def test_is_running_reads_the_gateway_pidfile(self) -> None:
        gw = FirecrackerGateway()
        with patch.object(infra_vm, "gateway_running", return_value=True):
            self.assertTrue(gw.is_running())
        with patch.object(infra_vm, "gateway_running", return_value=False):
            self.assertFalse(gw.is_running())

    def test_stop_stops_only_the_gateway_vm(self) -> None:
        gw = FirecrackerGateway()
        with patch.object(infra_vm, "stop_gateway") as stop:
            gw.stop()
        stop.assert_called_once_with()

    def test_address_is_the_gateway_link_guest_ip(self) -> None:
        gw = FirecrackerGateway()
        with patch.object(infra_vm, "gateway_guest_ip", return_value="10.243.255.3"):
            self.assertEqual("10.243.255.3", gw.address())

    def test_ca_cert_pem_uses_the_live_handle_when_present(self) -> None:
        vm = MagicMock()
        vm.gateway_ca_pem.return_value = "PEM"
        gw = FirecrackerGateway(vm)
        with patch.object(infra_vm, "adopted_gateway_vm") as adopt:
            self.assertEqual("PEM", gw.ca_cert_pem(timeout=1))
        adopt.assert_not_called()
        vm.gateway_ca_pem.assert_called_once_with(timeout=1)

    def test_ca_cert_pem_adopts_the_running_gateway_when_no_handle(self) -> None:
        vm = MagicMock()
        vm.gateway_ca_pem.return_value = "PEM"
        gw = FirecrackerGateway()
        with patch.object(infra_vm, "adopted_gateway_vm", return_value=vm) as adopt:
            self.assertEqual("PEM", gw.ca_cert_pem())
        adopt.assert_called_once_with()

    def test_provisioning_transport_targets_the_gateway_vm(self) -> None:
        gw = FirecrackerGateway()
        sentinel = object()
        with patch.object(infra_vm, "gateway_transport", return_value=sentinel):
            self.assertIs(sentinel, gw.provisioning_transport())


if __name__ == "__main__":
    unittest.main()
