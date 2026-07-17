"""Unit: the Apple gateway + orchestrator lifecycle (PRD 0070)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot_bottle.backend.macos_container.gateway import AppleGateway, GatewayError
from bot_bottle.backend.macos_container.orchestrator_service import (
    MacosOrchestratorService,
    OrchestratorStartError,
)

_GW = "bot_bottle.backend.macos_container.gateway"
_ORCH = "bot_bottle.backend.macos_container.orchestrator_service"


def _ok(stdout: str = "") -> Mock:
    return Mock(returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str = "boom") -> Mock:
    return Mock(returncode=1, stdout="", stderr=stderr)


class TestAppleGatewayRun(unittest.TestCase):
    def _argv(self, run: Mock) -> list[str]:
        return run.call_args.args[0]

    def _start(self, run: Mock) -> None:
        with patch(f"{_GW}.container_mod") as mod:
            mod.container_is_running.return_value = False
            mod.dns_server.return_value = "1.1.1.1"
            mod.run_container_argv = run
            AppleGateway(orchestrator_url="http://192.168.128.2:8099").ensure_running()

    def test_nat_network_precedes_the_host_only_network(self) -> None:
        """Apple Container makes the FIRST --network the default route, so the
        NAT network must lead or the gateway has no route out."""
        run = Mock(return_value=_ok())
        self._start(run)
        argv = self._argv(run)
        networks = [argv[i + 1] for i, a in enumerate(argv) if a == "--network"]
        self.assertEqual(["bot-bottle-mac-egress", "bot-bottle-mac-gateway"], networks)

    def test_control_plane_url_is_passed_for_multi_tenancy(self) -> None:
        run = Mock(return_value=_ok())
        self._start(run)
        self.assertIn(
            "BOT_BOTTLE_ORCHESTRATOR_URL=http://192.168.128.2:8099", self._argv(run),
        )

    def test_dns_is_explicit(self) -> None:
        """The NAT gateway routes but does not resolve."""
        run = Mock(return_value=_ok())
        self._start(run)
        argv = self._argv(run)
        self.assertEqual("1.1.1.1", argv[argv.index("--dns") + 1])

    def test_start_failure_raises(self) -> None:
        with self.assertRaises(GatewayError):
            self._start(Mock(return_value=_fail()))

    def test_running_current_gateway_is_left_alone(self) -> None:
        """Idempotent singleton: N launches must not restart the gateway and
        drop every other bottle's data plane."""
        run = Mock(return_value=_ok())
        with patch(f"{_GW}.container_mod") as mod:
            mod.container_is_running.return_value = True
            mod.container_image_digest.return_value = "abc"
            mod.image_digest.return_value = "abc"
            mod.run_container_argv = run
            AppleGateway().ensure_running()
        run.assert_not_called()

    def test_stale_image_forces_a_recreate(self) -> None:
        """A rebuilt image only takes effect if the running container is
        replaced — otherwise it keeps serving the OLD daemons."""
        run = Mock(return_value=_ok())
        with patch(f"{_GW}.container_mod") as mod:
            mod.container_is_running.return_value = True
            mod.container_image_digest.return_value = "old"
            mod.image_digest.return_value = "new"
            mod.dns_server.return_value = "1.1.1.1"
            mod.run_container_argv = run
            AppleGateway().ensure_running()
        run.assert_called_once()

    def test_unreadable_digest_does_not_churn(self) -> None:
        run = Mock(return_value=_ok())
        with patch(f"{_GW}.container_mod") as mod:
            mod.container_is_running.return_value = True
            mod.container_image_digest.return_value = ""
            mod.image_digest.return_value = ""
            mod.run_container_argv = run
            AppleGateway().ensure_running()
        run.assert_not_called()

    def _start_with_running_env(self, env: dict[str, str], url: str) -> Mock:
        run = Mock(return_value=_ok())
        with patch(f"{_GW}.container_mod") as mod:
            mod.container_is_running.return_value = True
            mod.container_image_digest.return_value = "abc"
            mod.image_digest.return_value = "abc"
            mod.container_env.return_value = env
            mod.dns_server.return_value = "1.1.1.1"
            mod.run_container_argv = run
            AppleGateway(orchestrator_url=url).ensure_running()
        return run

    def test_moved_control_plane_forces_a_recreate(self) -> None:
        """Docker hands the gateway a container *name*, stable across an
        orchestrator recreate. Apple has no DNS, so the URL is an IP baked into
        the gateway's env — if the orchestrator comes back on a new address and
        the gateway isn't recreated, every /resolve fails and every bottle on
        the host loses egress."""
        run = self._start_with_running_env(
            {"BOT_BOTTLE_ORCHESTRATOR_URL": "http://192.168.128.2:8099"},
            "http://192.168.128.7:8099",
        )
        run.assert_called_once()
        self.assertIn(
            "BOT_BOTTLE_ORCHESTRATOR_URL=http://192.168.128.7:8099",
            run.call_args.args[0],
        )

    def test_unmoved_control_plane_does_not_churn(self) -> None:
        run = self._start_with_running_env(
            {"BOT_BOTTLE_ORCHESTRATOR_URL": "http://192.168.128.2:8099"},
            "http://192.168.128.2:8099",
        )
        run.assert_not_called()

    def test_unreadable_env_does_not_churn(self) -> None:
        run = self._start_with_running_env({}, "http://192.168.128.2:8099")
        run.assert_not_called()


class TestMacosOrchestratorService(unittest.TestCase):
    def test_orchestrator_starts_before_the_gateway(self) -> None:
        """Apple has no container DNS, so the gateway can only be handed the
        control plane's IP — which does not exist until it is running. This
        ordering is the whole reason the macOS service diverges from docker's."""
        order: list[str] = []
        svc = MacosOrchestratorService(repo_root=Path("/r"), host_root=Path("/h"))
        gateway = Mock()
        gateway.ensure_running.side_effect = lambda: order.append("gateway")

        def _record_orchestrator(_hash: str) -> None:
            order.append("orchestrator")

        with patch(f"{_ORCH}.container_mod") as mod, \
                patch(f"{_ORCH}.source_hash", return_value="h1"), \
                patch.object(svc, "_run_orchestrator_container",
                             side_effect=_record_orchestrator), \
                patch.object(svc, "gateway", return_value=gateway), \
                patch.object(svc, "is_healthy", return_value=True):
            mod.container_is_running.return_value = False
            mod.image_exists.return_value = True
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            url = svc.ensure_running()
        self.assertEqual(["orchestrator", "gateway"], order)
        self.assertEqual("http://192.168.128.2:8099", url)

    def test_gateway_is_handed_the_resolved_url(self) -> None:
        svc = MacosOrchestratorService(repo_root=Path("/r"), host_root=Path("/h"))
        with patch(f"{_ORCH}.container_mod") as mod, \
                patch(f"{_ORCH}.source_hash", return_value="h1"), \
                patch.object(svc, "_run_orchestrator_container"), \
                patch.object(svc, "gateway") as gw, \
                patch.object(svc, "is_healthy", return_value=True):
            mod.container_is_running.return_value = False
            mod.image_exists.return_value = True
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            svc.ensure_running()
        gw.assert_called_with("http://192.168.128.2:8099")

    def test_current_source_leaves_a_healthy_orchestrator_alone(self) -> None:
        """Recreating on every launch would drop every other live bottle's
        in-memory egress tokens (#381)."""
        svc = MacosOrchestratorService(repo_root=Path("/r"), host_root=Path("/h"))
        run = Mock()
        with patch(f"{_ORCH}.container_mod") as mod, \
                patch(f"{_ORCH}.source_hash", return_value="h1"), \
                patch.object(svc, "_run_orchestrator_container", run), \
                patch.object(svc, "gateway"), \
                patch.object(svc, "is_healthy", return_value=True):
            mod.container_is_running.return_value = True
            mod.inspect_container.return_value = {
                "configuration": {"labels": {"bot-bottle-orchestrator-source-hash": "h1"}}
            }
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            svc.ensure_running()
        run.assert_not_called()

    def test_changed_source_recreates_the_orchestrator(self) -> None:
        """The control-plane process loaded its bind-mounted source at startup
        and won't reload it."""
        svc = MacosOrchestratorService(repo_root=Path("/r"), host_root=Path("/h"))
        run = Mock()
        with patch(f"{_ORCH}.container_mod") as mod, \
                patch(f"{_ORCH}.source_hash", return_value="h2"), \
                patch.object(svc, "_run_orchestrator_container", run), \
                patch.object(svc, "gateway"), \
                patch.object(svc, "is_healthy", return_value=True):
            mod.container_is_running.return_value = True
            mod.inspect_container.return_value = {
                "configuration": {"labels": {"bot-bottle-orchestrator-source-hash": "h1"}}
            }
            mod.image_exists.return_value = True
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            svc.ensure_running()
        run.assert_called_once()

    def test_never_healthy_raises(self) -> None:
        svc = MacosOrchestratorService(repo_root=Path("/r"), host_root=Path("/h"))
        with patch(f"{_ORCH}.container_mod") as mod, \
                patch(f"{_ORCH}.source_hash", return_value="h1"), \
                patch.object(svc, "_run_orchestrator_container"), \
                patch.object(svc, "is_healthy", return_value=False):
            mod.container_is_running.return_value = False
            mod.image_exists.return_value = True
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            with self.assertRaises(OrchestratorStartError):
                svc.ensure_running(startup_timeout=0.01)

    def test_control_plane_needs_no_route_out(self) -> None:
        """The orchestrator sits only on the host-only network: the host
        reaches it there directly, so there is no --publish and no NAT leg."""
        svc = MacosOrchestratorService(repo_root=Path("/r"), host_root=Path("/h"))
        run = Mock(return_value=_ok())
        # `ensure_networks` lives in the gateway module and shells out to the
        # `container` CLI, which does not exist on the Linux CI host — patch it
        # here, not gateway.container_mod, since it is called through this
        # module's imported name.
        with patch(f"{_ORCH}.container_mod") as mod, \
                patch(f"{_ORCH}.ensure_networks"):
            mod.run_container_argv = run
            svc._run_orchestrator_container("h1")
        argv = run.call_args.args[0]
        networks = [argv[i + 1] for i, a in enumerate(argv) if a == "--network"]
        self.assertEqual(["bot-bottle-mac-gateway"], networks)
        self.assertNotIn("--publish", argv)

    def test_networks_exist_before_the_orchestrator_runs(self) -> None:
        """The orchestrator is the first container on the shared network, so it
        has to create it — the gateway that used to do so now starts second."""
        svc = MacosOrchestratorService(repo_root=Path("/r"), host_root=Path("/h"))
        order: list[str] = []

        def _networks(*_args: str) -> None:
            order.append("networks")

        def _run(*_args: list[str]) -> Mock:
            order.append("run")
            return _ok()

        with patch(f"{_ORCH}.container_mod") as mod, \
                patch(f"{_ORCH}.ensure_networks", side_effect=_networks):
            mod.run_container_argv = Mock(side_effect=_run)
            svc._run_orchestrator_container("h1")
        self.assertEqual(["networks", "run"], order)

    def test_orchestrator_start_failure_raises(self) -> None:
        svc = MacosOrchestratorService(repo_root=Path("/r"), host_root=Path("/h"))
        with patch(f"{_ORCH}.container_mod") as mod, \
                patch(f"{_ORCH}.ensure_networks"):
            mod.run_container_argv = Mock(return_value=_fail())
            with self.assertRaises(OrchestratorStartError):
                svc._run_orchestrator_container("h1")


if __name__ == "__main__":
    unittest.main()
