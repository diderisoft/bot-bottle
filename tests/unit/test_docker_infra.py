"""Unit: orchestrator + gateway container lifecycle (PRD 0070 plane split).

`DockerInfraService` brings up two containers — the lean orchestrator on the
`--internal` control network + host loopback, and the dual-homed gateway. These
tests isolate the orchestrator-container logic by mocking `_gateway` (the
gateway runs via `DockerGateway`, exercised in its own test)."""

from __future__ import annotations

import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from bot_bottle.gateway import GatewayError
from bot_bottle.backend.docker.infra import (
    ORCHESTRATOR_NAME,
    ORCHESTRATOR_NETWORK,
    ORCHESTRATOR_SOURCE_HASH_LABEL,
    DockerInfraService,
)
from bot_bottle.gateway import GATEWAY_NAME
from bot_bottle.orchestrator.lifecycle import (
    OrchestratorStartError,
    source_hash,
)
from tests.unit import use_bottle_root

_URLOPEN = "bot_bottle.backend.docker.infra.urllib.request.urlopen"
_RUN = "bot_bottle.backend.docker.infra.run_docker"
_SLEEP = "bot_bottle.backend.docker.infra.time.sleep"
_MONOTONIC = "bot_bottle.backend.docker.infra.time.monotonic"


def _health(status: int) -> MagicMock:
    m = MagicMock()
    m.__enter__.return_value.status = status
    return m


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> Mock:
    return Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class TestDockerInfraService(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(use_bottle_root(Path(self._tmp.name)))
        self.svc = DockerInfraService(port=8099)
        # Isolate the orchestrator-container logic: the gateway is brought up by
        # DockerGateway (its own module/run_docker), tested separately.
        self.gw = MagicMock()
        patcher = patch.object(self.svc, "_gateway", return_value=self.gw)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_url(self) -> None:
        self.assertEqual("http://127.0.0.1:8099", self.svc.url)

    def test_gateway_name(self) -> None:
        self.assertEqual(GATEWAY_NAME, self.svc.gateway_name)

    def test_is_healthy(self) -> None:
        with patch(_URLOPEN, return_value=_health(200)):
            self.assertTrue(self.svc.is_healthy())
        with patch(_URLOPEN, side_effect=urllib.error.URLError("refused")):
            self.assertFalse(self.svc.is_healthy())

    def test_noop_when_healthy_and_source_unchanged(self) -> None:
        # A healthy orchestrator on current source is left alone — recreating it
        # on every launch drops in-memory egress tokens (#381). The gateway is
        # still refreshed (idempotent).
        current = source_hash(self.svc._repo_root)

        def fake(argv: list[str], **_kw: object) -> Mock:
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout=ORCHESTRATOR_NAME)
            if argv[:2] == ["docker", "inspect"]:
                return _proc(stdout=current)
            return _proc()

        with patch(_URLOPEN, return_value=_health(200)), \
                patch(_RUN, side_effect=fake) as run, patch(_SLEEP):
            self.assertEqual(self.svc.url, self.svc.ensure_running())
        runs = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["docker", "run"]]
        self.assertEqual([], runs)
        self.gw.ensure_running.assert_called_once_with()

    def test_recreates_orchestrator_when_source_changed(self) -> None:
        def fake(argv: list[str], **_kw: object) -> Mock:
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout=ORCHESTRATOR_NAME)
            if argv[:2] == ["docker", "inspect"]:
                return _proc(stdout="stale-hash")
            return _proc()

        with patch(_URLOPEN, return_value=_health(200)), \
                patch(_RUN, side_effect=fake) as run, patch(_SLEEP):
            self.assertEqual(self.svc.url, self.svc.ensure_running())
        runs = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["docker", "run"]]
        self.assertEqual(1, len(runs))
        self.assertIn(ORCHESTRATOR_NAME, runs[0])
        current = source_hash(self.svc._repo_root)
        self.assertIn(f"{ORCHESTRATOR_SOURCE_HASH_LABEL}={current}", runs[0])

    def test_starts_orchestrator_when_absent(self) -> None:
        def fake(argv: list[str], **_kw: object) -> Mock:
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout="")
            return _proc()

        with patch(_URLOPEN, side_effect=[urllib.error.URLError("down"), _health(200)]), \
                patch(_RUN, side_effect=fake) as run, patch(_SLEEP):
            self.assertEqual(self.svc.url, self.svc.ensure_running())
        runs = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["docker", "run"]]
        self.assertEqual(1, len(runs))
        argv = runs[0]
        self.assertIn(ORCHESTRATOR_NAME, argv)
        # Control network only — agents are never on it.
        self.assertEqual(ORCHESTRATOR_NETWORK, argv[argv.index("--network") + 1])
        # Published on loopback for the CLI, mapped to the fixed internal 8099.
        self.assertEqual("127.0.0.1:8099:8099", argv[argv.index("--publish") + 1])
        # The lean control plane: no mitmproxy CA mount, no gateway daemons.
        self.assertFalse([a for a in argv if a.endswith(":/home/mitmproxy/.mitmproxy")])
        self.assertNotIn("BOT_BOTTLE_GATEWAY_DAEMONS", " ".join(argv))
        # Orchestrator entrypoint args (image ENTRYPOINT is `-m bot_bottle.orchestrator`).
        self.assertIn("--broker", argv)
        self.assertIn("stub", argv)
        # Gateway is brought up after the control plane is healthy.
        self.gw.ensure_running.assert_called_once_with()

    def test_builds_gateway_and_orchestrator_images(self) -> None:
        def fake(argv: list[str], **_kw: object) -> Mock:
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout="")
            return _proc()

        with patch(_URLOPEN, side_effect=[urllib.error.URLError("down"), _health(200)]), \
                patch(_RUN, side_effect=fake) as run, patch(_SLEEP):
            self.svc.ensure_running()
        builds = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["docker", "build"]]
        # Two images now (the combined Dockerfile.infra is retired).
        self.assertEqual(2, len(builds))
        dockerfiles = [next(a for a in b if "Dockerfile" in a) for b in builds]
        self.assertIn("Dockerfile.gateway", dockerfiles[0])
        self.assertIn("Dockerfile.orchestrator", dockerfiles[1])

    def test_publish_maps_host_port_to_fixed_internal_port(self) -> None:
        def fake(argv: list[str], **_kw: object) -> Mock:
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout="")
            return _proc()

        svc = DockerInfraService(port=20001)
        with patch.object(svc, "_gateway", return_value=MagicMock()), \
                patch(_URLOPEN, side_effect=[urllib.error.URLError("down"), _health(200)]), \
                patch(_RUN, side_effect=fake) as run, patch(_SLEEP):
            svc.ensure_running()
        argv = next(c.args[0] for c in run.call_args_list if c.args[0][:2] == ["docker", "run"])
        self.assertEqual("127.0.0.1:20001:8099", argv[argv.index("--publish") + 1])

    def test_raises_on_control_plane_timeout(self) -> None:
        with patch(_URLOPEN, side_effect=urllib.error.URLError("down")), \
                patch(_RUN, return_value=_proc()), \
                patch(_SLEEP), patch(_MONOTONIC, side_effect=[0.0, 0.5, 2.0]):
            with self.assertRaises(OrchestratorStartError):
                self.svc.ensure_running(startup_timeout=1.0)

    def test_noop_when_healthy_and_inspect_fails(self) -> None:
        def fake(argv: list[str], **_kw: object) -> Mock:
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout=ORCHESTRATOR_NAME)
            if argv[:2] == ["docker", "inspect"]:
                return _proc(returncode=1, stderr="daemon error")
            return _proc()

        with patch(_URLOPEN, return_value=_health(200)), \
                patch(_RUN, side_effect=fake) as run, patch(_SLEEP):
            self.svc.ensure_running()
        runs = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["docker", "run"]]
        self.assertEqual([], runs)

    def test_build_failure_raises(self) -> None:
        with patch(_URLOPEN, side_effect=urllib.error.URLError("down")), \
                patch(_RUN, return_value=_proc(returncode=1, stderr="no space left")):
            with self.assertRaises(GatewayError):
                self.svc.ensure_running()

    def test_creates_control_network_internal(self) -> None:
        def fake(argv: list[str], **_kw: object) -> Mock:
            if argv[:3] == ["docker", "network", "inspect"]:
                return _proc(returncode=1, stderr="not found")
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout="")
            return _proc()

        with patch(_URLOPEN, side_effect=[urllib.error.URLError("down"), _health(200)]), \
                patch(_RUN, side_effect=fake) as run, patch(_SLEEP):
            self.svc.ensure_running()
        creates = [c.args[0] for c in run.call_args_list if c.args[0][:3] == ["docker", "network", "create"]]
        # The control network is created --internal; the data network isn't.
        control = [c for c in creates if ORCHESTRATOR_NETWORK in c]
        self.assertEqual(1, len(control))
        self.assertIn("--internal", control[0])

    def test_stop_removes_both_containers(self) -> None:
        with patch(_RUN) as run:
            self.svc.stop()
        rms = [
            c.args[0] for c in run.call_args_list
            if c.args[0][:3] == ["docker", "rm", "--force"]
        ]
        self.assertTrue(any(GATEWAY_NAME in a for a in rms))
        self.assertTrue(any(ORCHESTRATOR_NAME in a for a in rms))


if __name__ == "__main__":
    unittest.main()
