"""Unit: macOS orchestrator + gateway containers (PRD 0070 plane split)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from bot_bottle.backend.macos_container.infra import (
    INFRA_DB_VOLUME,
    MacosInfraService,
    OrchestratorStartError,
    probe_orchestrator_url,
)

_INFRA = "bot_bottle.backend.macos_container.infra"


def _ok(stdout: str = "") -> Mock:
    return Mock(returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str = "boom") -> Mock:
    return Mock(returncode=1, stdout="", stderr=stderr)


def _spec(src: str, tgt: str, readonly: bool = False) -> str:
    return f"type=bind,source={src},target={tgt}" + (",readonly" if readonly else "")


class TestOrchestratorRun(unittest.TestCase):
    def _run(self, svc: MacosInfraService) -> list[str]:
        run = Mock(return_value=_ok())
        with patch(f"{_INFRA}.container_mod") as mod, \
                patch(f"{_INFRA}.host_orchestrator_token", return_value="k"):
            mod.dns_server.return_value = "1.1.1.1"
            mod.bind_mount_spec.side_effect = _spec
            mod.run_container_argv = run
            svc._run_orchestrator_container("h1")
        return run.call_args.args[0]

    def test_runs_the_orchestrator_on_the_control_network_only(self) -> None:
        argv = self._run(MacosInfraService(repo_root=Path("/r")))
        nets = [argv[i + 1] for i, a in enumerate(argv) if a == "--network"]
        self.assertEqual(["bot-bottle-mac-control"], nets)
        # Image ENTRYPOINT is `-m bot_bottle.orchestrator`; these are its args.
        self.assertIn("--broker", argv)
        self.assertIn("stub", argv)
        self.assertIn("bot-bottle-orchestrator:latest", argv)

    def test_db_is_a_container_only_volume(self) -> None:
        argv = self._run(MacosInfraService(repo_root=Path("/r")))
        vols = [argv[i + 1] for i, a in enumerate(argv) if a == "--volume"]
        self.assertTrue(any(v.startswith(f"{INFRA_DB_VOLUME}:") for v in vols))

    def test_orchestrator_has_no_ca_mount(self) -> None:
        # The CA lives with the gateway, not the control plane.
        argv = self._run(MacosInfraService(repo_root=Path("/r")))
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
        self.assertFalse([m for m in mounts if "/home/mitmproxy" in m])

    def test_source_hash_is_labelled_for_recreate(self) -> None:
        argv = self._run(MacosInfraService(repo_root=Path("/r")))
        self.assertIn("BOT_BOTTLE_SOURCE_HASH=h1", argv)

    def test_start_failure_raises(self) -> None:
        svc = MacosInfraService(repo_root=Path("/r"))
        with patch(f"{_INFRA}.container_mod") as mod, \
                patch(f"{_INFRA}.host_orchestrator_token", return_value="k"):
            mod.dns_server.return_value = "1.1.1.1"
            mod.run_container_argv = Mock(return_value=_fail())
            with self.assertRaises(OrchestratorStartError):
                svc._run_orchestrator_container("h1")


class TestInfraEnsureRunning(unittest.TestCase):
    """Isolate the orchestrator-container logic; the gateway is brought up by
    `MacosGateway` (its own module, tested separately), so mock `svc.gateway`.
    `ensure_running` mints the `gateway` token, so `mint` /
    `host_orchestrator_token` are stubbed."""

    def _patches(self, svc: MacosInfraService):
        gw = MagicMock()
        return gw, (
            patch.object(svc, "ensure_built"),
            patch.object(svc, "gateway", return_value=gw),
            patch(f"{_INFRA}.host_orchestrator_token", return_value="k"),
            patch(f"{_INFRA}.mint", return_value="jwt"),
        )

    def test_current_healthy_orchestrator_left_alone(self) -> None:
        svc = MacosInfraService(repo_root=Path("/r"))
        run = Mock()
        gw, extra = self._patches(svc)
        with patch(f"{_INFRA}.container_mod") as mod, \
                patch(f"{_INFRA}.source_hash", return_value="h1"), \
                patch.object(svc, "_run_orchestrator_container", run), \
                patch.object(svc, "is_healthy", return_value=True), \
                extra[0], extra[1], extra[2], extra[3]:
            mod.container_is_running.return_value = True
            mod.container_env.return_value = {"BOT_BOTTLE_SOURCE_HASH": "h1"}
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            endpoint = svc.ensure_running()
        run.assert_not_called()
        gw.connect_to_orchestrator.assert_called_once()
        self.assertEqual("http://192.168.128.2:8099", endpoint.orchestrator_url)
        self.assertEqual("192.168.128.2", endpoint.gateway_ip)

    def test_changed_source_recreates_orchestrator(self) -> None:
        svc = MacosInfraService(repo_root=Path("/r"))
        run = Mock()
        _gw, extra = self._patches(svc)
        with patch(f"{_INFRA}.container_mod") as mod, \
                patch(f"{_INFRA}.source_hash", return_value="h2"), \
                patch.object(svc, "_run_orchestrator_container", run), \
                patch.object(svc, "is_healthy", return_value=True), \
                extra[0], extra[1], extra[2], extra[3]:
            mod.container_is_running.return_value = True
            mod.container_env.return_value = {"BOT_BOTTLE_SOURCE_HASH": "h1"}
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            svc.ensure_running()
        run.assert_called_once()

    def test_wedged_but_current_orchestrator_recreated(self) -> None:
        svc = MacosInfraService(repo_root=Path("/r"))
        run = Mock()
        _gw, extra = self._patches(svc)
        with patch(f"{_INFRA}.container_mod") as mod, \
                patch(f"{_INFRA}.source_hash", return_value="h1"), \
                patch.object(svc, "_run_orchestrator_container", run), \
                patch.object(svc, "is_healthy", Mock(side_effect=[False, True])), \
                extra[0], extra[1], extra[2], extra[3]:
            mod.container_is_running.return_value = True
            mod.container_env.return_value = {"BOT_BOTTLE_SOURCE_HASH": "h1"}
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            svc.ensure_running()
        run.assert_called_once()

    def test_never_healthy_raises(self) -> None:
        svc = MacosInfraService(repo_root=Path("/r"))
        _gw, extra = self._patches(svc)
        with patch(f"{_INFRA}.container_mod") as mod, \
                patch(f"{_INFRA}.source_hash", return_value="h1"), \
                patch.object(svc, "_run_orchestrator_container"), \
                patch.object(svc, "is_healthy", return_value=False), \
                extra[0], extra[1], extra[2], extra[3]:
            mod.container_is_running.return_value = False
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            with self.assertRaises(OrchestratorStartError):
                svc.ensure_running(startup_timeout=0.01)


class TestCaCertPem(unittest.TestCase):
    def test_delegates_to_the_gateway_service(self) -> None:
        svc = MacosInfraService(repo_root=Path("/r"))
        gw = MagicMock()
        gw.ca_cert_pem.return_value = "-----BEGIN CERTIFICATE-----\n"
        with patch.object(svc, "gateway", return_value=gw):
            pem = svc.ca_cert_pem(timeout=5)
        self.assertTrue(pem.startswith("-----BEGIN CERTIFICATE-----"))
        gw.ca_cert_pem.assert_called_once_with(timeout=5)


class TestProbeOrchestrator(unittest.TestCase):
    def test_returns_url_when_running(self) -> None:
        with patch(f"{_INFRA}.container_mod") as mod:
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            self.assertEqual("http://192.168.128.2:8099", probe_orchestrator_url())

    def test_empty_when_absent(self) -> None:
        with patch(f"{_INFRA}.container_mod") as mod:
            mod.try_container_ipv4_on_network.return_value = ""
            self.assertEqual("", probe_orchestrator_url())


if __name__ == "__main__":
    unittest.main()
