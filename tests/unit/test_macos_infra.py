"""Unit: the single macOS infra container (control plane + gateway, PRD 0070)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot_bottle.backend.macos_container.infra import (
    INFRA_DB_VOLUME,
    MacosInfraService,
    OrchestratorStartError,
    probe_control_plane_url,
)

_INFRA = "bot_bottle.backend.macos_container.infra"


def _ok(stdout: str = "") -> Mock:
    return Mock(returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str = "boom") -> Mock:
    return Mock(returncode=1, stdout="", stderr=stderr)


class TestInfraRun(unittest.TestCase):
    def _run_container(self, svc: MacosInfraService) -> list[str]:
        run = Mock(return_value=_ok())

        def _spec(src: str, tgt: str, readonly: bool = False) -> str:
            return f"type=bind,source={src},target={tgt}" + (
                ",readonly" if readonly else "")

        with patch(f"{_INFRA}.container_mod") as mod, \
                patch(f"{_INFRA}.ensure_networks"):
            mod.dns_server.return_value = "1.1.1.1"
            mod.bind_mount_spec.side_effect = _spec
            mod.run_container_argv = run
            svc._run_container("h1")
        return run.call_args.args[0]

    def test_single_container_runs_both_processes(self) -> None:
        """The whole point: one container starts the control plane AND the
        gateway daemons, so one kernel owns the DB."""
        argv = self._run_container(MacosInfraService(repo_root=Path("/r")))
        script = argv[-1]
        self.assertIn("bot_bottle.orchestrator", script)
        # Gateway launches via the installed package (there is no
        # /app/gateway_init.py file since the daemons moved into bot_bottle).
        self.assertIn("bot_bottle.gateway_init", script)
        self.assertIn("127.0.0.1", script)  # they reach each other on loopback

    def test_db_is_a_container_only_volume(self) -> None:
        """No host bind-mount of the DB — a named volume only this container
        mounts, so the DB is never written by two kernels."""
        argv = self._run_container(MacosInfraService(repo_root=Path("/r")))
        vols = [argv[i + 1] for i, a in enumerate(argv) if a == "--volume"]
        self.assertTrue(any(v.startswith(f"{INFRA_DB_VOLUME}:") for v in vols))
        # The repo source is bind-mounted read-only; the DB is not a bind mount.
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
        self.assertTrue(all("bot-bottle.db" not in m for m in mounts))

    def test_nat_network_precedes_the_host_only_network(self) -> None:
        argv = self._run_container(MacosInfraService(repo_root=Path("/r")))
        nets = [argv[i + 1] for i, a in enumerate(argv) if a == "--network"]
        self.assertEqual(["bot-bottle-mac-egress", "bot-bottle-mac-gateway"], nets)

    def test_source_hash_is_labelled_for_recreate(self) -> None:
        argv = self._run_container(MacosInfraService(repo_root=Path("/r")))
        self.assertIn("BOT_BOTTLE_SOURCE_HASH=h1", argv)

    def test_start_failure_raises(self) -> None:
        svc = MacosInfraService(repo_root=Path("/r"))
        with patch(f"{_INFRA}.container_mod") as mod, \
                patch(f"{_INFRA}.ensure_networks"):
            mod.dns_server.return_value = "1.1.1.1"
            mod.bind_mount_spec.return_value = "m"
            mod.run_container_argv = Mock(return_value=_fail())
            with self.assertRaises(OrchestratorStartError):
                svc._run_container("h1")


class TestInfraEnsureRunning(unittest.TestCase):
    def test_current_healthy_container_is_left_alone(self) -> None:
        """Idempotent singleton: N launches must not churn the infra container
        and drop every live bottle's control plane."""
        svc = MacosInfraService(repo_root=Path("/r"))
        run = Mock()
        with patch(f"{_INFRA}.container_mod") as mod, \
                patch(f"{_INFRA}.source_hash", return_value="h1"), \
                patch.object(svc, "_run_container", run), \
                patch.object(svc, "is_healthy", return_value=True):
            mod.container_is_running.return_value = True
            mod.container_env.return_value = {"BOT_BOTTLE_SOURCE_HASH": "h1"}
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            endpoint = svc.ensure_running()
        run.assert_not_called()
        self.assertEqual("http://192.168.128.2:8099", endpoint.control_plane_url)
        self.assertEqual("192.168.128.2", endpoint.gateway_ip)

    def test_changed_source_recreates(self) -> None:
        svc = MacosInfraService(repo_root=Path("/r"))
        run = Mock()
        with patch(f"{_INFRA}.container_mod") as mod, \
                patch(f"{_INFRA}.source_hash", return_value="h2"), \
                patch.object(svc, "ensure_built"), \
                patch.object(svc, "_run_container", run), \
                patch.object(svc, "is_healthy", return_value=True):
            mod.container_is_running.return_value = True
            mod.container_env.return_value = {"BOT_BOTTLE_SOURCE_HASH": "h1"}
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            svc.ensure_running()
        run.assert_called_once()

    def test_wedged_but_current_container_is_recreated(self) -> None:
        """Current source but a dead HTTP server must be recreated, not polled
        to death forever — health, not just the source label, gates reuse."""
        svc = MacosInfraService(repo_root=Path("/r"))
        run = Mock()
        health = Mock(side_effect=[False, True])
        with patch(f"{_INFRA}.container_mod") as mod, \
                patch(f"{_INFRA}.source_hash", return_value="h1"), \
                patch.object(svc, "ensure_built"), \
                patch.object(svc, "_run_container", run), \
                patch.object(svc, "is_healthy", health):
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
                patch.object(svc, "_run_container"), \
                patch.object(svc, "is_healthy", return_value=False):
            mod.container_is_running.return_value = False
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            with self.assertRaises(OrchestratorStartError):
                svc.ensure_running(startup_timeout=0.01)


class TestCaCertPem(unittest.TestCase):
    def test_reads_ca_out_of_the_container(self) -> None:
        svc = MacosInfraService(repo_root=Path("/r"))
        with patch(f"{_INFRA}.container_mod") as mod:
            mod.run_container_argv.return_value = _ok("-----BEGIN CERTIFICATE-----\n")
            pem = svc.ca_cert_pem()
        self.assertTrue(pem.startswith("-----BEGIN CERTIFICATE-----"))
        argv = mod.run_container_argv.call_args.args[0]
        self.assertEqual(["container", "exec", "bot-bottle-mac-infra", "cat"], argv[:4])


class TestProbeControlPlane(unittest.TestCase):
    def test_returns_url_when_running(self) -> None:
        with patch(f"{_INFRA}.container_mod") as mod:
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            self.assertEqual("http://192.168.128.2:8099", probe_control_plane_url())

    def test_empty_when_absent(self) -> None:
        with patch(f"{_INFRA}.container_mod") as mod:
            mod.try_container_ipv4_on_network.return_value = ""
            self.assertEqual("", probe_control_plane_url())


if __name__ == "__main__":
    unittest.main()
