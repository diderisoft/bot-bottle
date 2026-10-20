"""Integration: the consolidated Docker gateway is an idempotent singleton.

Gated on a reachable Docker daemon. Uses a unique name so it never
collides with a real per-host gateway or a leftover from another run.
"""

from __future__ import annotations

import secrets
import subprocess
import unittest

from bot_bottle.cli.settings import settings_from_env
from bot_bottle.backend.docker.gateway import DockerGateway
from tests._backend import skip_unless_backend

IMAGE = "busybox"


@skip_unless_backend("docker")
class TestDockerGatewayIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.name = "bot-bottle-orch-gateway-itest-" + secrets.token_hex(4)
        self.sc = DockerGateway(IMAGE, name=self.name, settings=settings_from_env())
        self.addCleanup(self.sc.stop)

    def _connect(self) -> None:
        # Resolver-only data plane (PRD 0070) requires an orchestrator URL + a
        # pre-minted `gateway` token to run; busybox never dials the orchestrator
        # or presents the token, so placeholders are enough here.
        self.sc.connect_to_orchestrator("http://orchestrator:9000", "placeholder-token")

    def _count(self) -> int:
        proc = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=^{self.name}$",
             "--format", "{{.Names}}"],
            stdout=subprocess.PIPE, text=True, check=False,
        )
        return sum(1 for n in proc.stdout.split() if n == self.name)

    def test_ensure_is_idempotent_singleton(self) -> None:
        self._connect()
        self.assertEqual(1, self._count())
        # A second connect must not spawn a second container — one per host.
        self._connect()
        self.assertEqual(1, self._count())
        self.sc.stop()
        self.assertEqual(0, self._count())


if __name__ == "__main__":
    unittest.main()
