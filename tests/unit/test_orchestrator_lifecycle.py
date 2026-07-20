"""Unit: infra container lifecycle — idempotent singleton (PRD 0070)."""

from __future__ import annotations

import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from bot_bottle.orchestrator.gateway import GatewayError
from bot_bottle.orchestrator.lifecycle import (
    INFRA_NAME,
    INFRA_SOURCE_HASH_LABEL,
    OrchestratorService,
    OrchestratorStartError,
    source_hash,
)
from tests.unit import use_bottle_root

_URLOPEN = "bot_bottle.orchestrator.lifecycle.urllib.request.urlopen"
_RUN = "bot_bottle.orchestrator.lifecycle.run_docker"
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

    def test_url(self) -> None:
        self.assertEqual("http://127.0.0.1:8099", self.svc.url)

    def test_is_healthy(self) -> None:
        with patch(_URLOPEN, return_value=_health(200)):
            self.assertTrue(self.svc.is_healthy())
        with patch(_URLOPEN, side_effect=urllib.error.URLError("refused")):
            self.assertFalse(self.svc.is_healthy())

    def test_ensure_running_noop_when_healthy_and_source_unchanged(self) -> None:
        # A healthy container on current source is left alone — recreating it
        # on every launch drops in-memory egress tokens (#381).
        current = source_hash(self.svc._repo_root)
        calls: list[list[str]] = []

        def fake(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout=INFRA_NAME)
            if argv[:2] == ["docker", "inspect"]:
                return _proc(stdout=current)
            return _proc()

        with patch(_URLOPEN, return_value=_health(200)), \
                patch(_RUN, side_effect=fake), patch(_SLEEP):
            self.assertEqual(self.svc.url, self.svc.ensure_running())
        runs = [c for c in calls if c[:2] == ["docker", "run"]]
        rms = [c for c in calls if c[:3] == ["docker", "rm", "--force"] and INFRA_NAME in c]
        self.assertEqual([], runs)
        self.assertEqual([], rms)

    def test_ensure_running_recreates_when_source_changed(self) -> None:
        calls: list[list[str]] = []

        def fake(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout=INFRA_NAME)
            if argv[:2] == ["docker", "inspect"]:
                return _proc(stdout="stale-hash")
            return _proc()

        with patch(_URLOPEN, return_value=_health(200)), \
                patch(_RUN, side_effect=fake), patch(_SLEEP):
            self.assertEqual(self.svc.url, self.svc.ensure_running())
        runs = [c for c in calls if c[:2] == ["docker", "run"]]
        self.assertEqual(1, len(runs))
        self.assertIn(INFRA_NAME, runs[0])
        current = source_hash(self.svc._repo_root)
        self.assertIn(f"{INFRA_SOURCE_HASH_LABEL}={current}", runs[0])

    def test_ensure_running_starts_infra_container_when_absent(self) -> None:
        calls: list[list[str]] = []

        def fake(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout="")
            return _proc()

        with patch(_URLOPEN, side_effect=[urllib.error.URLError("down"), _health(200)]), \
                patch(_RUN, side_effect=fake), patch(_SLEEP):
            self.assertEqual(self.svc.url, self.svc.ensure_running())
        runs = [c for c in calls if c[:2] == ["docker", "run"]]
        self.assertEqual(1, len(runs))
        argv = runs[0]
        self.assertIn(INFRA_NAME, argv)
        # Published on loopback — not exposed on external interfaces.
        self.assertEqual("127.0.0.1:8099:8099", argv[argv.index("--publish") + 1])
        # Both processes in one container — no separate entrypoint override.
        self.assertNotIn("--entrypoint", argv)
        # Gateway daemons + orchestrator explicitly opted in.
        daemons_flag = "BOT_BOTTLE_GATEWAY_DAEMONS=egress,git-http,supervise,orchestrator"
        self.assertIn("orchestrator", argv[argv.index(daemons_flag)])

    def test_ensure_running_builds_both_images(self) -> None:
        calls: list[list[str]] = []

        def fake(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout="")
            return _proc()

        with patch(_URLOPEN, side_effect=[urllib.error.URLError("down"), _health(200)]), \
                patch(_RUN, side_effect=fake), patch(_SLEEP):
            self.svc.ensure_running()
        builds = [c for c in calls if c[:2] == ["docker", "build"]]
        # Orchestrator (build intermediate) + infra image both built.
        self.assertEqual(2, len(builds))
        dockerfiles = [next(a for a in b if "Dockerfile" in a) for b in builds]
        self.assertIn("Dockerfile.orchestrator", dockerfiles[0])
        self.assertIn("Dockerfile.infra", dockerfiles[1])
        # Images are distinct — the point of the split.
        tags = [b[b.index("-t") + 1] for b in builds]
        self.assertNotEqual(tags[0], tags[1])

    def test_ensure_running_raises_on_timeout(self) -> None:
        with patch(_URLOPEN, side_effect=urllib.error.URLError("down")), \
                patch(_RUN, return_value=Mock(returncode=0, stdout="", stderr="")), \
                patch(_SLEEP), patch(_MONOTONIC, side_effect=[0.0, 0.5, 2.0]):
            with self.assertRaises(OrchestratorStartError):
                self.svc.ensure_running(startup_timeout=1.0)

    def test_noop_when_healthy_and_inspect_fails(self) -> None:
        """If docker inspect fails (e.g. docker daemon hiccup), leave the
        working container alone rather than churning it."""
        def fake(argv: list[str], **_kw: object) -> Mock:
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout=INFRA_NAME)
            if argv[:2] == ["docker", "inspect"]:
                return _proc(returncode=1, stderr="daemon error")
            return _proc()

        with patch(_URLOPEN, return_value=_health(200)), \
                patch(_RUN, side_effect=fake), patch(_SLEEP):
            self.svc.ensure_running()
        # no docker run — the working container was left alone

    def test_build_failure_raises(self) -> None:
        with patch(_URLOPEN, side_effect=urllib.error.URLError("down")), \
                patch(_RUN, return_value=_proc(returncode=1, stderr="no space left on device")):
            with self.assertRaises(GatewayError):
                self.svc.ensure_running()

    def test_ensure_network_creates_if_missing(self) -> None:
        """If the gateway network doesn't exist yet, create it."""
        calls: list[list[str]] = []

        def fake(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            if argv[:3] == ["docker", "network", "inspect"]:
                return _proc(returncode=1, stderr="not found")
            if argv[:2] == ["docker", "ps"]:
                return _proc(stdout="")
            return _proc()

        with patch(_URLOPEN, side_effect=[urllib.error.URLError("down"), _health(200)]), \
                patch(_RUN, side_effect=fake), patch(_SLEEP):
            self.svc.ensure_running()
        creates = [c for c in calls if c[:3] == ["docker", "network", "create"]]
        self.assertEqual(1, len(creates))

    def test_stop_removes_infra_container(self) -> None:
        with patch(_RUN) as run:
            self.svc.stop()
        rms = [
            c.args[0] for c in run.call_args_list
            if c.args[0][:3] == ["docker", "rm", "--force"]
        ]
        self.assertTrue(any(INFRA_NAME in a for a in rms))


if __name__ == "__main__":
    unittest.main()
