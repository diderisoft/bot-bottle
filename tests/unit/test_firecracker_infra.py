"""Unit: FirecrackerInfraService composes the orchestrator + gateway microVM pair.

The two-VM singleton lifecycle (adopt-or-boot-both under a flock, the version
marker) that must hold without a live VM. The VM substrate + boot primitives are
tested in test_firecracker_infra_vm; the orchestrator/gateway services in their
own modules. These isolate the *composition*."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.contract.hostctl import Settings
from bot_bottle.backend.firecracker import infra_vm
from bot_bottle.backend.firecracker.gateway import FirecrackerGateway
from bot_bottle.backend.firecracker.infra import FirecrackerInfraService
from bot_bottle.backend.firecracker.orchestrator import FirecrackerOrchestrator

# The service imports the classes into its own module namespace, so patch them
# there (not at their home modules).
_ORCH_CLS = "bot_bottle.backend.firecracker.infra.FirecrackerOrchestrator"
_GW_CLS = "bot_bottle.backend.firecracker.infra.FirecrackerGateway"
# The shared bring-up harness reattaches through these (PRD 0081).
_REATTACH = "bot_bottle.backend.infra_service.reattach_running_bottles"
_ROOT = "bot_bottle.backend.infra_service.bot_bottle_root"


class TestAccessors(unittest.TestCase):
    def test_orchestrator_and_gateway_services(self) -> None:
        svc = FirecrackerInfraService(Settings())
        self.assertIsInstance(svc.orchestrator(), FirecrackerOrchestrator)
        self.assertIsInstance(svc.gateway(), FirecrackerGateway)

    def test_stop_stops_both_vms(self) -> None:
        with patch.object(infra_vm, "stop") as stop:
            FirecrackerInfraService(Settings()).stop()
        stop.assert_called_once()

    def test_url_and_is_healthy_delegate_to_the_orchestrator(self) -> None:
        svc = FirecrackerInfraService(Settings())
        ip = infra_vm.netpool.from_settings(Settings().infra).orch_slot().guest_ip
        self.assertEqual(f"http://{ip}:{infra_vm.ORCHESTRATOR_PORT}", svc.url())


class TestEnsureRunning(unittest.TestCase):
    def test_adopts_when_healthy_alive_and_version_matches(self):
        # Healthy control plane + live gateway + existing key + matching version
        # marker -> adopt (no stop/build/boot); returns the orchestrator URL.
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "id_ed25519").write_text("k")
            (d / "booted-version").write_text("v-current\n")
            with patch.object(infra_vm, "_infra_dir", return_value=d), \
                    patch.object(infra_vm, "expected_version", return_value="v-current"), \
                    patch.object(infra_vm, "_health_ok", return_value=True), \
                    patch.object(infra_vm, "_pidfile_alive", return_value=True), \
                    patch.object(infra_vm, "stop") as stop, \
                    patch.object(infra_vm, "ensure_built") as built, \
                    patch(_REATTACH) as reconcile, patch(_ROOT, return_value=Path(td)):
                url = FirecrackerInfraService(Settings()).ensure_running()
            stop.assert_not_called()
            built.assert_not_called()
            # Adopt path: no reconcile — gateway state is intact.
            reconcile.assert_not_called()
            ip = infra_vm.netpool.from_settings(Settings().infra).orch_slot().guest_ip
            self.assertEqual(f"http://{ip}:{infra_vm.ORCHESTRATOR_PORT}", url)

    def test_reboots_both_when_version_stale(self):
        # Healthy control plane but the running pair booted an OLDER image
        # (marker mismatch) -> reboot rather than adopt stale code.
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "id_ed25519").write_text("k")
            (d / "booted-version").write_text("v-old\n")
            with patch.object(infra_vm, "_infra_dir", return_value=d), \
                    patch.object(infra_vm, "expected_version", return_value="v-current"), \
                    patch.object(infra_vm, "_health_ok", return_value=True), \
                    patch.object(infra_vm, "_pidfile_alive", return_value=True), \
                    patch.object(infra_vm, "stop") as stop, \
                    patch.object(infra_vm, "ensure_built"), \
                    patch(_ORCH_CLS) as orch_cls, patch(_GW_CLS) as gw_cls, \
                    patch(_REATTACH) as reconcile, patch(_ROOT, return_value=Path(td)):
                orch_cls.return_value.gateway_url.return_value = "http://10.243.255.1:8099"
                orch_cls.return_value.mint_gateway_token.return_value = "gw.jwt"
                FirecrackerInfraService(Settings()).ensure_running()
            stop.assert_called_once()          # dislodge the outdated pair
            orch_cls.return_value.ensure_running.assert_called_once()
            # The gateway service is bound to the orchestrator's gateway URL,
            # carrying the orchestrator-minted `gateway` token.
            gw_cls.return_value.connect_to_orchestrator.assert_called_once_with(
                "http://10.243.255.1:8099", "gw.jwt")
            # The fresh boot records the current version for the next launcher.
            self.assertEqual("v-current\n", (d / "booted-version").read_text())
            # Cold boot: reconcile fires after gateway is up.
            reconcile.assert_called_once()

    def test_reboots_when_gateway_dead(self):
        # Orchestrator healthy + marker current, but the gateway VM is gone ->
        # the pair is half-down, so reboot both rather than adopt partial state.
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "id_ed25519").write_text("k")
            (d / "booted-version").write_text("v-current\n")
            with patch.object(infra_vm, "_infra_dir", return_value=d), \
                    patch.object(infra_vm, "expected_version", return_value="v-current"), \
                    patch.object(infra_vm, "_health_ok", return_value=True), \
                    patch.object(infra_vm, "_pidfile_alive", return_value=False), \
                    patch.object(infra_vm, "stop") as stop, \
                    patch.object(infra_vm, "ensure_built"), \
                    patch(_ORCH_CLS) as orch_cls, patch(_GW_CLS) as gw_cls, \
                    patch(_REATTACH) as reconcile, patch(_ROOT, return_value=Path(td)):
                FirecrackerInfraService(Settings()).ensure_running()
            stop.assert_called_once()
            orch_cls.return_value.ensure_running.assert_called_once()
            gw_cls.return_value.connect_to_orchestrator.assert_called_once()
            reconcile.assert_called_once()

    def test_boots_both_when_no_running_pair(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(infra_vm, "_infra_dir", return_value=Path(td)), \
                    patch.object(infra_vm, "expected_version", return_value="v-current"), \
                    patch.object(infra_vm, "_health_ok", return_value=False), \
                    patch.object(infra_vm, "stop") as stop, \
                    patch.object(infra_vm, "ensure_built") as built, \
                    patch(_ORCH_CLS) as orch_cls, patch(_GW_CLS) as gw_cls, \
                    patch(_REATTACH) as reconcile, patch(_ROOT, return_value=Path(td)):
                FirecrackerInfraService(Settings()).ensure_running()
        stop.assert_called_once()      # clear stale VMs first
        built.assert_called_once()
        # Orchestrator is brought up BEFORE the gateway is connected (its daemons
        # reach the control plane at startup).
        orch_cls.return_value.ensure_running.assert_called_once()
        gw_cls.return_value.connect_to_orchestrator.assert_called_once()
        # Cold boot: reconcile fires to restore CA, git-gate, and egress tokens.
        reconcile.assert_called_once()

    def test_cold_boot_reattaches_running_bottles(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(infra_vm, "_infra_dir", return_value=Path(td)), \
                    patch.object(infra_vm, "expected_version", return_value="v-current"), \
                    patch.object(infra_vm, "_health_ok", return_value=False), \
                    patch.object(infra_vm, "stop"), \
                    patch.object(infra_vm, "ensure_built"), \
                    patch(_ORCH_CLS) as orch_cls, patch(_GW_CLS), \
                    patch(_REATTACH) as reconcile, patch(_ROOT, return_value=Path(td)):
                orch_cls.return_value.url.return_value = "http://10.243.255.1:8099"
                FirecrackerInfraService(Settings()).ensure_running()
        # Both VMs booted cold, so every running bottle is reattached against
        # the orchestrator's host URL.
        reconcile.assert_called_once()
        self.assertEqual("http://10.243.255.1:8099", reconcile.call_args.kwargs["orchestrator_url"])

if __name__ == "__main__":
    unittest.main()
