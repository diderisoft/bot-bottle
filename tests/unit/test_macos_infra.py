"""Unit: macOS orchestrator + gateway containers (PRD 0070 plane split)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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


class TestGatewayRun(unittest.TestCase):
    def _run(self, svc: MacosInfraService, url: str = "http://10.0.0.5:8099") -> list[str]:
        run = Mock(return_value=_ok())
        with patch(f"{_INFRA}.container_mod") as mod, \
                patch(f"{_INFRA}.host_orchestrator_token", return_value="k"), \
                patch(f"{_INFRA}.mint", return_value="jwt"):
            mod.dns_server.return_value = "1.1.1.1"
            mod.bind_mount_spec.side_effect = _spec
            mod.run_container_argv = run
            svc._ensure_gateway_container(url)
        return run.call_args.args[0]

    def test_gateway_is_triple_homed(self) -> None:
        argv = self._run(MacosInfraService(repo_root=Path("/r")))
        nets = [argv[i + 1] for i, a in enumerate(argv) if a == "--network"]
        self.assertEqual(
            ["bot-bottle-mac-egress", "bot-bottle-mac-gateway", "bot-bottle-mac-control"],
            nets,
        )

    def test_gateway_resolves_orchestrator_by_url_and_holds_ca(self) -> None:
        argv = self._run(MacosInfraService(repo_root=Path("/r")), "http://10.0.0.5:8099")
        self.assertIn("BOT_BOTTLE_ORCHESTRATOR_URL=http://10.0.0.5:8099", argv)
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
        self.assertTrue([m for m in mounts if "target=/home/mitmproxy/.mitmproxy" in m])
        self.assertIn("bot-bottle-gateway:latest", argv)
        self.assertIn(
            "BOT_BOTTLE_GATEWAY_DAEMONS=egress,git-http,supervise", argv)


class TestInfraEnsureRunning(unittest.TestCase):
    def test_current_healthy_orchestrator_left_alone(self) -> None:
        svc = MacosInfraService(repo_root=Path("/r"))
        run = Mock()
        with patch(f"{_INFRA}.container_mod") as mod, \
                patch(f"{_INFRA}.source_hash", return_value="h1"), \
                patch.object(svc, "ensure_built"), \
                patch.object(svc, "_run_orchestrator_container", run), \
                patch.object(svc, "_ensure_gateway_container"), \
                patch.object(svc, "is_healthy", return_value=True):
            mod.container_is_running.return_value = True
            mod.container_env.return_value = {"BOT_BOTTLE_SOURCE_HASH": "h1"}
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            endpoint = svc.ensure_running()
        run.assert_not_called()
        self.assertEqual("http://192.168.128.2:8099", endpoint.orchestrator_url)
        self.assertEqual("192.168.128.2", endpoint.gateway_ip)

    def test_changed_source_recreates_orchestrator(self) -> None:
        svc = MacosInfraService(repo_root=Path("/r"))
        run = Mock()
        with patch(f"{_INFRA}.container_mod") as mod, \
                patch(f"{_INFRA}.source_hash", return_value="h2"), \
                patch.object(svc, "ensure_built"), \
                patch.object(svc, "_run_orchestrator_container", run), \
                patch.object(svc, "_ensure_gateway_container"), \
                patch.object(svc, "is_healthy", return_value=True):
            mod.container_is_running.return_value = True
            mod.container_env.return_value = {"BOT_BOTTLE_SOURCE_HASH": "h1"}
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            svc.ensure_running()
        run.assert_called_once()

    def test_wedged_but_current_orchestrator_recreated(self) -> None:
        svc = MacosInfraService(repo_root=Path("/r"))
        run = Mock()
        with patch(f"{_INFRA}.container_mod") as mod, \
                patch(f"{_INFRA}.source_hash", return_value="h1"), \
                patch.object(svc, "ensure_built"), \
                patch.object(svc, "_run_orchestrator_container", run), \
                patch.object(svc, "_ensure_gateway_container"), \
                patch.object(svc, "is_healthy", Mock(side_effect=[False, True])):
            mod.container_is_running.return_value = True
            mod.container_env.return_value = {"BOT_BOTTLE_SOURCE_HASH": "h1"}
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            svc.ensure_running()
        run.assert_called_once()

    def test_never_healthy_raises(self) -> None:
        svc = MacosInfraService(repo_root=Path("/r"))
        with patch(f"{_INFRA}.container_mod") as mod, \
                patch(f"{_INFRA}.source_hash", return_value="h1"), \
                patch.object(svc, "ensure_built"), \
                patch.object(svc, "_run_orchestrator_container"), \
                patch.object(svc, "_ensure_gateway_container"), \
                patch.object(svc, "is_healthy", return_value=False):
            mod.container_is_running.return_value = False
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            with self.assertRaises(OrchestratorStartError):
                svc.ensure_running(startup_timeout=0.01)


class TestCaCertPem(unittest.TestCase):
    def test_reads_ca_out_of_the_gateway_container(self) -> None:
        svc = MacosInfraService(repo_root=Path("/r"))
        with patch(f"{_INFRA}.container_mod") as mod:
            mod.run_container_argv.return_value = _ok("-----BEGIN CERTIFICATE-----\n")
            pem = svc.ca_cert_pem()
        self.assertTrue(pem.startswith("-----BEGIN CERTIFICATE-----"))
        argv = mod.run_container_argv.call_args.args[0]
        self.assertEqual(["container", "exec", "bot-bottle-mac-infra", "cat"], argv[:4])

    def test_raises_gateway_error_when_cert_never_appears(self) -> None:
        from bot_bottle.backend.macos_container.gateway import GatewayError
        svc = MacosInfraService(repo_root=Path("/r"))
        with patch(f"{_INFRA}.container_mod") as mod:
            mod.run_container_argv.return_value = _fail()
            with self.assertRaises(GatewayError):
                svc.ca_cert_pem(timeout=0)


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
