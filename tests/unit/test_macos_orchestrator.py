"""Unit: the macOS orchestrator (control plane) container lifecycle (PRD 0070)."""

from __future__ import annotations

import dataclasses

import unittest
from typing import Any
from pathlib import Path
from unittest.mock import Mock, patch

from bot_bottle.contract.hostctl import Settings
from bot_bottle.backend.macos_container.orchestrator import (
    ORCHESTRATOR_DB_VOLUME,
    ORCHESTRATOR_NAME,
    MacosOrchestrator,
    probe_orchestrator_url,
)
from bot_bottle.control.lifecycle import OrchestratorStartError

_ORCH = "bot_bottle.backend.macos_container.orchestrator"


def _ok(stdout: str = "") -> Mock:
    return Mock(returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str = "boom") -> Mock:
    return Mock(returncode=1, stdout="", stderr=stderr)


def _spec(src: str, tgt: str, readonly: bool = False) -> str:
    return f"type=bind,source={src},target={tgt}" + (",readonly" if readonly else "")


class TestMacosOrchestratorRun(unittest.TestCase):
    def _run(self) -> list[str]:
        run = Mock(return_value=_ok())
        with patch(f"{_ORCH}.container_mod") as mod, \
                patch("bot_bottle.trust_domain.host_signing_key", return_value="k"):
            mod.dns_server.return_value = "1.1.1.1"
            mod.bind_mount_spec.side_effect = _spec
            mod.run_container_argv = run
            MacosOrchestrator(repo_root=Path("/r"), settings=Settings())._run_container("h1")
        return run.call_args.args[0]

    def test_runs_on_the_control_network_only(self) -> None:
        argv = self._run()
        nets = [argv[i + 1] for i, a in enumerate(argv) if a == "--network"]
        self.assertEqual(["bot-bottle-mac-control"], nets)
        # Image ENTRYPOINT is `-m bot_bottle.orchestrator`; these are its args.
        self.assertNotIn("--broker", argv)
        self.assertIn("bot-bottle-orchestrator:latest", argv)
        self.assertEqual("/var/lib/bot-bottle/db/bot-bottle.db",
                         argv[argv.index("--db") + 1])

    def test_db_is_a_container_only_volume(self) -> None:
        argv = self._run()
        vols = [argv[i + 1] for i, a in enumerate(argv) if a == "--volume"]
        self.assertTrue(any(v.startswith(f"{ORCHESTRATOR_DB_VOLUME}:") for v in vols))

    def test_no_ca_mount(self) -> None:
        # The CA lives with the gateway, not the control plane.
        argv = self._run()
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
        self.assertFalse([m for m in mounts if "/home/mitmproxy" in m])

    def test_source_hash_is_labelled_for_recreate(self) -> None:
        self.assertIn("BOT_BOTTLE_SOURCE_HASH=h1", self._run())

    def test_start_failure_raises(self) -> None:
        with patch(f"{_ORCH}.container_mod") as mod, \
                patch("bot_bottle.trust_domain.host_signing_key", return_value="k"):
            mod.dns_server.return_value = "1.1.1.1"
            mod.run_container_argv = Mock(return_value=_fail())
            with self.assertRaises(OrchestratorStartError):
                MacosOrchestrator(repo_root=Path("/r"), settings=Settings())._run_container("h1")


class TestMacosOrchestratorUrls(unittest.TestCase):
    def test_url_and_gateway_url_are_the_control_net_address(self) -> None:
        orch = MacosOrchestrator(port=8099, settings=Settings())
        with patch(f"{_ORCH}.container_mod") as mod:
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            self.assertEqual("http://192.168.128.2:8099", orch.url())
            self.assertEqual("http://192.168.128.2:8099", orch.gateway_url())

    def test_url_empty_when_no_address(self) -> None:
        orch = MacosOrchestrator(settings=Settings())
        with patch(f"{_ORCH}.container_mod") as mod:
            mod.try_container_ipv4_on_network.return_value = ""
            self.assertEqual("", orch.url())

    def test_is_healthy_false_when_no_address(self) -> None:
        orch = MacosOrchestrator(settings=Settings())
        with patch(f"{_ORCH}.container_mod") as mod:
            mod.try_container_ipv4_on_network.return_value = ""
            self.assertFalse(orch.is_healthy())


class TestMacosOrchestratorEnsureRunning(unittest.TestCase):
    def _orch(self) -> MacosOrchestrator:
        return MacosOrchestrator(repo_root=Path("/r"), settings=Settings())

    def test_noop_when_current_and_healthy(self) -> None:
        orch = self._orch()
        with patch(f"{_ORCH}.source_hash", return_value="h1"), \
                patch.object(orch, "_source_current", return_value=True), \
                patch.object(orch, "is_healthy", return_value=True), \
                patch.object(orch, "_run_container") as run:
            orch.ensure_running()
        run.assert_not_called()

    def test_recreates_when_source_changed(self) -> None:
        orch = self._orch()
        with patch(f"{_ORCH}.source_hash", return_value="h2"), \
                patch.object(orch, "_source_current", return_value=False), \
                patch.object(orch, "_run_container") as run, \
                patch.object(orch, "_wait_healthy"):
            orch.ensure_running()
        run.assert_called_once()

    def test_recreates_when_current_but_wedged(self) -> None:
        orch = self._orch()
        with patch(f"{_ORCH}.source_hash", return_value="h1"), \
                patch.object(orch, "_source_current", return_value=True), \
                patch.object(orch, "is_healthy", return_value=False), \
                patch.object(orch, "_run_container") as run, \
                patch.object(orch, "_wait_healthy"):
            orch.ensure_running()
        run.assert_called_once()

    def test_never_healthy_raises(self) -> None:
        orch = self._orch()
        with patch(f"{_ORCH}.source_hash", return_value="h1"), \
                patch.object(orch, "_source_current", return_value=False), \
                patch.object(orch, "_run_container"), \
                patch.object(orch, "health_problem", return_value="connection refused"), \
                patch(f"{_ORCH}.time.sleep"), \
                patch(f"{_ORCH}.time.monotonic", side_effect=[0.0, 0.5, 2.0]):
            with self.assertRaisesRegex(OrchestratorStartError, "connection refused"):
                orch.ensure_running(startup_timeout=1.0)

    def test_unreachable_host_points_at_local_network_privacy(self) -> None:
        # A Python macOS hasn't granted Local Network access gets EHOSTUNREACH
        # to vmnet addresses that curl reaches fine.
        orch = self._orch()
        with patch.object(orch, "health_problem",
                          return_value="http://192.168.128.2:8099/health: "
                                       "[Errno 65] No route to host"), \
                patch(f"{_ORCH}.time.sleep"), \
                patch(f"{_ORCH}.time.monotonic", side_effect=[0.0, 2.0]):
            with self.assertRaisesRegex(OrchestratorStartError, "Local Network"):
                orch._wait_healthy(1.0)  # pyright: ignore[reportPrivateUsage]


class TestMacosOrchestratorBuildStop(unittest.TestCase):
    def build(self, *, have_build: bool, no_cache: bool = False) -> tuple[MacosOrchestrator, Any]:
        settings = Settings()
        if no_cache:
            settings = dataclasses.replace(
                settings, operation=dataclasses.replace(settings.operation, no_cache=True))
        orch = MacosOrchestrator(repo_root=Path("/r"), settings=settings)
        with patch(f"{_ORCH}.container_mod") as mod, \
                patch(f"{_ORCH}.pinned_infra_artifact", return_value=None), \
                patch(f"{_ORCH}.local_build_tag", return_value="bot-bottle-orchestrator:src-abc"):
            mod.image_exists.return_value = have_build
            orch.ensure_built()
        return orch, mod

    def test_ensure_built_builds_and_tags_the_orchestrator_image(self) -> None:
        orch, mod = self.build(have_build=False)
        _args, kwargs = mod.build_image.call_args
        self.assertEqual("Dockerfile.orchestrator", kwargs["dockerfile"])
        mod.tag_image.assert_called_once_with(mod.build_image.call_args[0][0],
                                              "bot-bottle-orchestrator:src-abc")
        self.assertEqual("bot-bottle-orchestrator:src-abc", orch.image_ref)

    def test_a_build_of_this_code_is_reused(self) -> None:
        orch, mod = self.build(have_build=True)
        mod.build_image.assert_not_called()
        self.assertEqual("bot-bottle-orchestrator:src-abc", orch.image_ref)

    def test_no_cache_builds_anyway(self) -> None:
        _orch, mod = self.build(have_build=True, no_cache=True)
        mod.build_image.assert_called_once()

    def test_stop_removes_the_container(self) -> None:
        orch = MacosOrchestrator(settings=Settings())
        with patch(f"{_ORCH}.container_mod") as mod:
            orch.stop()
        mod.force_remove_container.assert_called_once_with(ORCHESTRATOR_NAME)


class TestProbeOrchestrator(unittest.TestCase):
    def test_returns_url_when_running(self) -> None:
        with patch(f"{_ORCH}.container_mod") as mod:
            mod.try_container_ipv4_on_network.return_value = "192.168.128.2"
            self.assertEqual("http://192.168.128.2:8099", probe_orchestrator_url())

    def test_empty_when_absent(self) -> None:
        with patch(f"{_ORCH}.container_mod") as mod:
            mod.try_container_ipv4_on_network.return_value = ""
            self.assertEqual("", probe_orchestrator_url())


if __name__ == "__main__":
    unittest.main()
