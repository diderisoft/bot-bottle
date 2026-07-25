"""Unit: the Firecracker gateway data plane as a microVM (PRD 0070).

The gateway service owns its host-side logic directly: booting the gateway VM
(via the shared `infra_vm.boot_vm` substrate), seeding the pre-minted token,
fetching the CA over SSH, and the SSH exec/cp provisioning transport.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from bot_bottle.backend.firecracker import infra_vm
from bot_bottle.backend.firecracker.gateway import (
    GATEWAY_NAME,
    FirecrackerGateway,
)
from bot_bottle.backend.firecracker.gateway_transport import (
    FirecrackerGatewayTransport,
)
from bot_bottle.gateway import GatewayError, GatewayProvisionError

_GW = "bot_bottle.backend.firecracker.gateway"
_GW_TRANSPORT = "bot_bottle.backend.firecracker.gateway_transport"

_ORCH_URL = "http://10.243.255.1:8099"
_TOKEN = "gw-jwt-token"


class TestFirecrackerGatewayConnect(unittest.TestCase):
    def test_default_name(self) -> None:
        self.assertEqual(GATEWAY_NAME, FirecrackerGateway().name)

    def test_refuses_without_orchestrator_url(self) -> None:
        gw = FirecrackerGateway()
        with patch.object(infra_vm, "boot_vm") as boot:
            with self.assertRaises(GatewayError):
                gw.connect_to_orchestrator("", _TOKEN)
            boot.assert_not_called()

    def test_refuses_without_gateway_token(self) -> None:
        gw = FirecrackerGateway()
        with patch.object(infra_vm, "boot_vm") as boot:
            with self.assertRaises(GatewayError):
                gw.connect_to_orchestrator(_ORCH_URL, "")
            boot.assert_not_called()

    def test_refuses_a_url_without_a_host(self) -> None:
        gw = FirecrackerGateway()
        with patch.object(infra_vm, "boot_vm") as boot:
            with self.assertRaises(GatewayError):
                gw.connect_to_orchestrator("http://:8099", _TOKEN)
            boot.assert_not_called()

    def test_boots_the_gateway_vm_and_seeds_the_token(self) -> None:
        gw = FirecrackerGateway()
        booted = infra_vm.InfraVm(guest_ip="10.243.255.3", private_key=Path("/k"))
        with patch.object(infra_vm, "boot_vm", return_value=booted) as boot, \
                patch.object(infra_vm, "push_secret") as push:
            gw.connect_to_orchestrator(_ORCH_URL, _TOKEN)
        # Booted on the gateway link with the gateway role; the orchestrator's
        # guest IP (parsed off the URL) rides the cmdline as bb_orch.
        kw = boot.call_args.kwargs
        self.assertEqual("gateway", kw["role"])
        self.assertIn("bb_orch=10.243.255.1", kw["extra_boot_args"])
        self.assertIsNone(kw.get("data_drive"))  # data plane never opens the DB
        # The host-minted token (never the key — #469) is pushed to the guest.
        push.assert_called_once()
        self.assertEqual(_TOKEN, push.call_args.args[1])


class TestFirecrackerGatewaySurface(unittest.TestCase):
    def test_is_running_reads_the_gateway_pidfile(self) -> None:
        gw = FirecrackerGateway()
        with patch.object(infra_vm, "_pidfile_alive", return_value=True) as alive, \
                patch.object(infra_vm, "_gw_dir", return_value=Path("/gw")):
            self.assertTrue(gw.is_running())
        alive.assert_called_once_with(Path("/gw"))

    def test_stop_kills_only_the_gateway_pidfile(self) -> None:
        gw = FirecrackerGateway()
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "vm.pid").write_text("123")
            with patch.object(infra_vm, "_gw_dir", return_value=d), \
                    patch.object(infra_vm, "_kill_pidfile") as kill:
                gw.stop()
            kill.assert_called_once_with(d)
            self.assertFalse((d / "vm.pid").exists())  # pidfile cleared

    def test_address_is_the_gateway_link_guest_ip(self) -> None:
        gw = FirecrackerGateway()
        self.assertEqual(infra_vm.netpool.gw_slot().guest_ip, gw.address())

    def test_ca_cert_pem_reads_over_ssh_from_the_handle(self) -> None:
        vm = infra_vm.InfraVm(guest_ip="10.243.255.3", private_key=Path("/k"))
        gw = FirecrackerGateway(vm)
        pem = "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n"
        with patch.object(infra_vm.subprocess, "run",
                          return_value=CompletedProcess([], 0, stdout=pem)) as run:
            self.assertEqual(pem, gw.ca_cert_pem(timeout=5))
        # cat the CA path over SSH to the gateway VM's guest IP.
        argv = run.call_args.args[0]
        self.assertTrue(any("10.243.255.3" in a for a in argv))
        self.assertIn("cat /home/mitmproxy/.mitmproxy/mitmproxy-ca-cert.pem", argv)

    def test_ca_cert_pem_raises_gateway_error_when_absent(self) -> None:
        gw = FirecrackerGateway()
        with patch.object(infra_vm.subprocess, "run",
                          return_value=CompletedProcess([], 1, stdout="", stderr="")):
            with self.assertRaises(GatewayError):
                gw.ca_cert_pem(timeout=0)

    def test_provisioning_transport_targets_the_gateway_link(self) -> None:
        gw = FirecrackerGateway()
        transport = gw.provisioning_transport()
        assert isinstance(transport, FirecrackerGatewayTransport)
        self.assertEqual(infra_vm.netpool.gw_slot().guest_ip, transport._ip)


class TestFirecrackerGatewayTransport(unittest.TestCase):
    def test_cp_into_preserves_source_mode(self) -> None:
        with tempfile.NamedTemporaryFile() as f:
            os.chmod(f.name, 0o700)  # like the staged access-hook
            t = FirecrackerGatewayTransport(Path("/k"), "10.0.0.1")
            with patch(f"{_GW_TRANSPORT}.subprocess.run",
                       return_value=CompletedProcess([], 0)) as run:
                t.cp_into(f.name, "/etc/git-gate/access-hook")
        remote_cmd = run.call_args.args[0][-1]
        self.assertIn("chmod 700", remote_cmd)  # exec bit preserved over SSH

    def test_exec_raises_on_failure(self) -> None:
        t = FirecrackerGatewayTransport(Path("/k"), "10.0.0.1")
        with patch(f"{_GW_TRANSPORT}.subprocess.run",
                   return_value=CompletedProcess([], 1, stderr="nope")), \
                self.assertRaises(GatewayProvisionError):
            t.exec(["mkdir", "-p", "/git-gate"])


if __name__ == "__main__":
    unittest.main()
