"""Unit: orchestrator+gateway container lifecycle — idempotent singleton (PRD 0070)."""

from __future__ import annotations

import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from bot_bottle.orchestrator.lifecycle import (
    ORCHESTRATOR_IMAGE,
    ORCHESTRATOR_NAME,
    ORCHESTRATOR_SOURCE_HASH_LABEL,
    OrchestratorService,
    OrchestratorStartError,
    source_hash,
)
from tests.unit import use_bottle_root

_URLOPEN = "bot_bottle.orchestrator.lifecycle.urllib.request.urlopen"
_RUN = "bot_bottle.orchestrator.lifecycle.run_docker"
_GATEWAY = "bot_bottle.orchestrator.lifecycle.DockerGateway"
_SLEEP = "bot_bottle.orchestrator.lifecycle.time.sleep"
_MONOTONIC = "bot_bottle.orchestrator.lifecycle.time.monotonic"


def _health(status: int) -> MagicMock:
    m = MagicMock()
    m.__enter__.return_value.status = status
    return m


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> Mock:
    return Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class TestOrchestratorService(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(use_bottle_root(Path(self._tmp.name)))
        self.svc = OrchestratorService(port=8099)

    def test_urls(self) -> None:
        self.assertEqual("http://127.0.0.1:8099", self.svc.url)
        # The gateway reaches the control plane by container name over docker DNS.
        self.assertEqual(f"http://{ORCHESTRATOR_NAME}:8099", self.svc.internal_url)

    def test_is_healthy(self) -> None:
        with patch(_URLOPEN, return_value=_health(200)):
            self.assertTrue(self.svc.is_healthy())
        with patch(_URLOPEN, side_effect=urllib.error.URLError("refused")):
            self.assertFalse(self.svc.is_healthy())

    def test_ensure_running_noop_when_healthy_and_source_unchanged(self) -> None:
        # A healthy control plane already running the *current* bind-mounted
        # source is left alone — recreating it on every launch would drop
        # every other active bottle's in-memory egress tokens (#381).
        current = source_hash(self.svc._repo_root)
        calls: list[list[str]] = []

        def fake(argv: list[str]) -> Mock:
            calls.append(argv)
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout=ORCHESTRATOR_NAME)
            if argv[:2] == ["docker", "inspect"]:
                return _proc(stdout=current)
            return _proc()

        with patch(_URLOPEN, return_value=_health(200)), \
                patch(_GATEWAY) as gw_cls, patch(_RUN, side_effect=fake), patch(_SLEEP):
            self.assertEqual(self.svc.url, self.svc.ensure_running())
        gw_cls.return_value.ensure_running.assert_called()   # gateway kept up
        runs = [c for c in calls if c[:2] == ["docker", "run"]]
        rms = [c for c in calls if c[:3] == ["docker", "rm", "--force"] and ORCHESTRATOR_NAME in c]
        self.assertEqual([], runs)   # not recreated
        self.assertEqual([], rms)

    def test_ensure_running_recreates_when_source_changed(self) -> None:
        # Healthy, but the running container's label doesn't match the
        # current source hash (a real code change) — recreate so it takes
        # effect, same as the gateway's image-staleness check.
        calls: list[list[str]] = []

        def fake(argv: list[str]) -> Mock:
            calls.append(argv)
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout=ORCHESTRATOR_NAME)
            if argv[:2] == ["docker", "inspect"]:
                return _proc(stdout="stale-hash")
            return _proc()

        with patch(_URLOPEN, return_value=_health(200)), \
                patch(_GATEWAY), patch(_RUN, side_effect=fake), patch(_SLEEP):
            self.assertEqual(self.svc.url, self.svc.ensure_running())
        runs = [c for c in calls if c[:2] == ["docker", "run"]]
        self.assertEqual(1, len(runs))
        self.assertIn(ORCHESTRATOR_NAME, runs[0])
        # the fresh container is labeled with the current hash, not the stale one
        current = source_hash(self.svc._repo_root)
        self.assertIn(f"{ORCHESTRATOR_SOURCE_HASH_LABEL}={current}", runs[0])

    def test_ensure_running_starts_orchestrator_container_when_absent(self) -> None:
        calls: list[list[str]] = []

        def fake(argv: list[str]) -> Mock:
            calls.append(argv)
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout="")   # not running
            return _proc()

        with patch(_URLOPEN, side_effect=[urllib.error.URLError("down"), _health(200)]), \
                patch(_GATEWAY), patch(_RUN, side_effect=fake), patch(_SLEEP):
            self.assertEqual(self.svc.url, self.svc.ensure_running())
        runs = [c for c in calls if c[:2] == ["docker", "run"]]
        self.assertEqual(1, len(runs))
        argv = runs[0]
        self.assertIn(ORCHESTRATOR_NAME, argv)
        self.assertIn("--broker", argv)
        self.assertEqual("stub", argv[argv.index("--broker") + 1])  # register-only, no socket
        self.assertIn("bot_bottle.orchestrator", argv)
        self.assertEqual("127.0.0.1:8099:8099", argv[argv.index("--publish") + 1])

    def test_ensure_running_builds_lean_orchestrator_image_when_missing(self) -> None:
        # The control plane runs its own lean image (#384), distinct from the
        # gateway data plane — built from Dockerfile.orchestrator when absent.
        calls: list[list[str]] = []

        def fake(argv: list[str]) -> Mock:
            calls.append(argv)
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout="")                 # orchestrator not running
            if argv[:3] == ["docker", "image", "inspect"]:
                return _proc(returncode=1)               # image absent -> build
            return _proc()

        with patch(_URLOPEN, side_effect=[urllib.error.URLError("down"), _health(200)]), \
                patch(_GATEWAY), patch(_RUN, side_effect=fake), patch(_SLEEP):
            self.svc.ensure_running()
        builds = [c for c in calls if c[:2] == ["docker", "build"]]
        self.assertEqual(1, len(builds))
        self.assertIn(ORCHESTRATOR_IMAGE, builds[0])
        self.assertTrue(any(a.endswith("Dockerfile.orchestrator") for a in builds[0]))
        # It is NOT the gateway image/dockerfile — the split is the point.
        self.assertFalse(any("Dockerfile.gateway" in a for a in builds[0]))

    def test_ensure_running_skips_orchestrator_image_build_when_present(self) -> None:
        calls: list[list[str]] = []

        def fake(argv: list[str]) -> Mock:
            calls.append(argv)
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout="")
            if argv[:3] == ["docker", "image", "inspect"]:
                return _proc(returncode=0)               # image present -> no build
            return _proc()

        with patch(_URLOPEN, side_effect=[urllib.error.URLError("down"), _health(200)]), \
                patch(_GATEWAY), patch(_RUN, side_effect=fake), patch(_SLEEP):
            self.svc.ensure_running()
        self.assertEqual([], [c for c in calls if c[:2] == ["docker", "build"]])

    def test_ensure_running_raises_on_timeout(self) -> None:
        with patch(_URLOPEN, side_effect=urllib.error.URLError("down")), \
                patch(_GATEWAY), patch(_RUN, return_value=Mock(returncode=0, stderr="")), \
                patch(_SLEEP), patch(_MONOTONIC, side_effect=[0.0, 0.5, 2.0]):
            with self.assertRaises(OrchestratorStartError):
                self.svc.ensure_running(startup_timeout=1.0)

    def test_stop_removes_orchestrator_and_gateway(self) -> None:
        with patch(_RUN) as run, patch(_GATEWAY) as gw_cls:
            self.svc.stop()
        rms = [c.args[0] for c in run.call_args_list if c.args[0][:3] == ["docker", "rm", "--force"]]
        self.assertTrue(any(ORCHESTRATOR_NAME in a for a in rms))
        gw_cls.return_value.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
