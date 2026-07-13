"""Integration: the consolidated Docker sidecar is an idempotent singleton.

Gated on a reachable Docker daemon. Uses a unique name so it never
collides with a real per-host sidecar or a leftover from another run.
"""

from __future__ import annotations

import secrets
import subprocess
import unittest

from bot_bottle.orchestrator.sidecar import DockerSidecar
from tests._docker import skip_unless_docker

IMAGE = "busybox"


@skip_unless_docker()
class TestDockerSidecarIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.name = "bot-bottle-orch-sidecar-itest-" + secrets.token_hex(4)
        self.sc = DockerSidecar(IMAGE, name=self.name)
        self.addCleanup(self.sc.stop)

    def _count(self) -> int:
        proc = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=^{self.name}$",
             "--format", "{{.Names}}"],
            stdout=subprocess.PIPE, text=True, check=False,
        )
        return sum(1 for n in proc.stdout.split() if n == self.name)

    def test_ensure_is_idempotent_singleton(self) -> None:
        self.sc.ensure_running()
        self.assertEqual(1, self._count())
        # A second ensure must not spawn a second container — one per host.
        self.sc.ensure_running()
        self.assertEqual(1, self._count())
        self.sc.stop()
        self.assertEqual(0, self._count())


if __name__ == "__main__":
    unittest.main()
