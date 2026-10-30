"""Unit: MacosInfraService composes the orchestrator + gateway services.

`MacosInfraService` no longer owns the container lifecycles — it composes the
`MacosOrchestrator` (control plane) and `MacosGateway` (data plane) services,
each tested in its own module. These tests isolate the *composition*: the
networks are ensured, both images built, the orchestrator comes up first, then
the gateway is connected to it with a freshly minted token."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.contract.hostctl import Settings
from bot_bottle.backend.macos_container.infra import MacosInfraService

_INFRA = "bot_bottle.backend.macos_container.infra"


class TestInfraEnsureRunning(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = MacosInfraService(Settings(), repo_root=Path("/r"))
        self.orch = MagicMock()
        self.orch.url.return_value = "http://192.168.128.2:8099"
        self.orch.mint_gateway_token.return_value = "gw.jwt"
        self.gw = MagicMock()
        for name, mock in (("orchestrator", self.orch), ("gateway", self.gw)):
            p = patch.object(self.svc, name, return_value=mock)
            p.start()
            self.addCleanup(p.stop)
        # Reattaching itself is covered by the shared harness's tests.
        reattach = patch("bot_bottle.backend.infra_service.reattach_running_bottles")
        self.reattach = reattach.start()
        self.addCleanup(reattach.stop)
        # A release matches: building locally is opt-in (test_infra_build_opt_in).
        matches = patch("bot_bottle.backend.infra_service.published_infra_matches",
                        return_value=True)
        matches.start()
        self.addCleanup(matches.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = patch("bot_bottle.backend.infra_service.bot_bottle_root", return_value=Path(tmp.name))
        root.start()
        self.addCleanup(root.stop)

    def test_composes_networks_builds_and_brings_up_orchestrator_first(self) -> None:
        with patch(f"{_INFRA}.ensure_networks") as nets:
            url = self.svc.ensure_running()
        nets.assert_called_once()
        self.orch.ensure_built.assert_called_once()
        self.gw.ensure_built.assert_called_once()
        self.orch.ensure_running.assert_called_once()
        # Returns the host control-plane URL (the InfraService contract).
        self.assertEqual("http://192.168.128.2:8099", url)

    def test_connects_gateway_with_orchestrator_url_and_minted_token(self) -> None:
        with patch(f"{_INFRA}.ensure_networks"):
            self.svc.ensure_running()
        self.gw.connect_to_orchestrator.assert_called_once_with(
            "http://192.168.128.2:8099", "gw.jwt")
        self.orch.mint_gateway_token.assert_called_once()

    def test_bring_up_reports_whether_either_plane_restarted(self) -> None:
        # The shared harness reattaches on True; adopting both planes is False.
        for orch_restarted, gw_restarted in ((False, False), (True, False), (False, True)):
            with self.subTest(orch=orch_restarted, gw=gw_restarted):
                self.orch.ensure_running.return_value = orch_restarted
                self.gw.connect_to_orchestrator.return_value = gw_restarted
                with patch(f"{_INFRA}.ensure_networks"):
                    url, restarted = self.svc._bring_up(startup_timeout=1)
                self.assertEqual("http://192.168.128.2:8099", url)
                self.assertEqual(orch_restarted or gw_restarted, restarted)

    def test_is_healthy_delegates_to_the_orchestrator(self) -> None:
        self.orch.is_healthy.return_value = True
        self.assertTrue(self.svc.is_healthy())
        self.orch.is_healthy.assert_called_once()


class TestCaCertPem(unittest.TestCase):
    def test_delegates_to_the_gateway_service(self) -> None:
        svc = MacosInfraService(Settings(), repo_root=Path("/r"))
        gw = MagicMock()
        gw.ca_cert_pem.return_value = "-----BEGIN CERTIFICATE-----\n"
        with patch.object(svc, "gateway", return_value=gw):
            pem = svc.ca_cert_pem(timeout=5)
        self.assertTrue(pem.startswith("-----BEGIN CERTIFICATE-----"))
        gw.ca_cert_pem.assert_called_once_with(timeout=5)


class TestStop(unittest.TestCase):
    def test_removes_both_containers(self) -> None:
        svc = MacosInfraService(Settings(), repo_root=Path("/r"))
        with patch(f"{_INFRA}.container_mod") as mod:
            svc.stop()
        removed = {c.args[0] for c in mod.force_remove_container.call_args_list}
        self.assertIn("bot-bottle-mac-infra", removed)       # gateway
        self.assertIn("bot-bottle-mac-orchestrator", removed)  # orchestrator


if __name__ == "__main__":
    unittest.main()
