"""Unit: the `rotate_ca` one-shot CLI (issue #450). Docker mocked."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot_bottle.orchestrator import rotate_ca
from bot_bottle.gateway import GATEWAY_NAME
from bot_bottle.backend.docker.infra import INFRA_NAME
from bot_bottle.paths import host_gateway_ca_dir
from tests.unit import use_bottle_root

_RUN = "bot_bottle.orchestrator.rotate_ca.run_docker"


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> Mock:
    return Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class TestRotateCaCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(use_bottle_root(Path(self._tmp.name)))

    def test_clears_ca_and_drops_gateway_containers(self) -> None:
        ca_dir = host_gateway_ca_dir()
        (ca_dir / "mitmproxy-ca.pem").write_text("x")
        (ca_dir / "mitmproxy-ca-cert.pem").write_text("x")

        calls: list[list[str]] = []

        def fake(argv: list[str], **_kw: object) -> Mock:
            calls.append(argv)
            # Report a removed container name so the CLI logs it.
            return _proc(stdout=argv[-1])

        with patch(_RUN, side_effect=fake):
            self.assertEqual(0, rotate_ca.main([]))

        # Persisted CA is gone → next start remints it.
        self.assertEqual([], list(ca_dir.glob("mitmproxy-ca*")))
        # Both the infra container and the standalone gateway are force-removed
        # so no mitmproxy keeps serving the old CA from memory.
        removed = {c[-1] for c in calls if c[:3] == ["docker", "rm", "--force"]}
        self.assertEqual({INFRA_NAME, GATEWAY_NAME}, removed)

    def test_succeeds_with_no_persisted_ca(self) -> None:
        with patch(_RUN, return_value=_proc()) as m:
            self.assertEqual(0, rotate_ca.main([]))
        # Still tears down any running gateway even when there was no CA on disk.
        self.assertTrue(m.called)


if __name__ == "__main__":
    unittest.main()
