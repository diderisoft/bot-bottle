"""Unit: orchestrator process lifecycle — idempotent singleton (PRD 0070)."""

from __future__ import annotations

import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.orchestrator.lifecycle import (
    OrchestratorProcess,
    OrchestratorStartError,
)
from tests.unit import use_bottle_root

_URLOPEN = "bot_bottle.orchestrator.lifecycle.urllib.request.urlopen"
_POPEN = "bot_bottle.orchestrator.lifecycle.subprocess.Popen"
_SLEEP = "bot_bottle.orchestrator.lifecycle.time.sleep"
_MONOTONIC = "bot_bottle.orchestrator.lifecycle.time.monotonic"


def _health(status: int) -> MagicMock:
    """A urlopen() context-manager whose `.status` is `status`."""
    m = MagicMock()
    m.__enter__.return_value.status = status
    return m


class TestOrchestratorProcess(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(use_bottle_root(Path(self._tmp.name)))
        self.p = OrchestratorProcess(port=8099)

    def test_url(self) -> None:
        self.assertEqual("http://127.0.0.1:8099", self.p.url)

    def test_is_healthy_true_on_200(self) -> None:
        with patch(_URLOPEN, return_value=_health(200)):
            self.assertTrue(self.p.is_healthy())

    def test_is_healthy_false_on_error(self) -> None:
        with patch(_URLOPEN, side_effect=urllib.error.URLError("refused")):
            self.assertFalse(self.p.is_healthy())

    def test_ensure_running_noop_when_already_healthy(self) -> None:
        with patch(_URLOPEN, return_value=_health(200)), patch(_POPEN) as popen:
            self.assertEqual(self.p.url, self.p.ensure_running())
        popen.assert_not_called()  # a live control plane is left untouched

    def test_ensure_running_spawns_then_waits_for_health(self) -> None:
        # First check (before spawn) fails; after spawn the poll succeeds.
        with patch(_URLOPEN, side_effect=[urllib.error.URLError("down"), _health(200)]), \
                patch(_POPEN) as popen, patch(_SLEEP):
            url = self.p.ensure_running()
        self.assertEqual(self.p.url, url)
        popen.assert_called_once()

    def test_ensure_running_raises_on_startup_timeout(self) -> None:
        with patch(_URLOPEN, side_effect=urllib.error.URLError("down")), \
                patch(_POPEN), patch(_SLEEP), \
                patch(_MONOTONIC, side_effect=[0.0, 0.5, 2.0]):
            with self.assertRaises(OrchestratorStartError):
                self.p.ensure_running(startup_timeout=1.0)

    def test_argv_includes_gateway_and_broker(self) -> None:
        argv = OrchestratorProcess(port=8099, broker="docker", gateway=True)._argv()
        self.assertIn("--gateway", argv)
        self.assertIn("bot_bottle.orchestrator", argv)
        self.assertEqual("docker", argv[argv.index("--broker") + 1])

    def test_argv_omits_gateway_when_disabled(self) -> None:
        self.assertNotIn("--gateway", OrchestratorProcess(gateway=False)._argv())

    def test_spawn_launches_detached_and_logs(self) -> None:
        with patch(_POPEN) as popen:
            self.p._spawn()
        popen.assert_called_once()
        kwargs = popen.call_args.kwargs
        self.assertTrue(kwargs["start_new_session"])  # outlives the CLI
        self.assertTrue((Path(self._tmp.name) / "orchestrator.log").exists())


if __name__ == "__main__":
    unittest.main()
