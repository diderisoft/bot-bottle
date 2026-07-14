"""Unit: orchestrator+gateway container lifecycle — idempotent singleton (PRD 0070)."""

from __future__ import annotations

import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from bot_bottle.orchestrator.lifecycle import (
    ORCHESTRATOR_NAME,
    OrchestratorService,
    OrchestratorStartError,
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

    def test_ensure_running_always_recreates_orchestrator(self) -> None:
        # Even when a control plane is already healthy, the orchestrator is
        # recreated so bind-mounted code changes take effect (its process
        # won't reload). The gateway is ensured too.
        run = Mock(return_value=Mock(returncode=0, stderr=""))
        with patch(_URLOPEN, return_value=_health(200)), \
                patch(_GATEWAY) as gw_cls, patch(_RUN, run), patch(_SLEEP):
            self.assertEqual(self.svc.url, self.svc.ensure_running())
        gw_cls.return_value.ensure_running.assert_called()   # gateway kept up
        runs = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["docker", "run"]]
        self.assertEqual(1, len(runs))                       # orchestrator recreated
        self.assertIn(ORCHESTRATOR_NAME, runs[0])

    def test_ensure_running_starts_orchestrator_container_when_absent(self) -> None:
        run = Mock(return_value=Mock(returncode=0, stderr=""))
        with patch(_URLOPEN, side_effect=[urllib.error.URLError("down"), _health(200)]), \
                patch(_GATEWAY), patch(_RUN, run), patch(_SLEEP):
            self.assertEqual(self.svc.url, self.svc.ensure_running())
        runs = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["docker", "run"]]
        self.assertEqual(1, len(runs))
        argv = runs[0]
        self.assertIn(ORCHESTRATOR_NAME, argv)
        self.assertIn("--broker", argv)
        self.assertEqual("stub", argv[argv.index("--broker") + 1])  # register-only, no socket
        self.assertIn("bot_bottle.orchestrator", argv)
        self.assertEqual("127.0.0.1:8099:8099", argv[argv.index("--publish") + 1])

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
