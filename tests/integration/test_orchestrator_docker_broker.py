"""Integration: the Docker launch broker starts and removes a real container.

Gated on a reachable Docker daemon (skips cleanly otherwise). Uses a tiny
image; the container may exit immediately — we only assert it exists after
launch and is gone after teardown.
"""

from __future__ import annotations

import secrets
import subprocess
import unittest

from bot_bottle.orchestrator.broker import LaunchRequest, sign_request
from bot_bottle.orchestrator.docker_broker import DockerBroker, container_name
from tests._docker import skip_unless_docker

IMAGE = "busybox"


@skip_unless_docker()
class TestDockerBrokerIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = secrets.token_bytes(16)
        self.broker = DockerBroker(self.secret)
        self.bottle_id = "itest" + secrets.token_hex(4)
        self.name = container_name(self.bottle_id)
        self.addCleanup(
            lambda: subprocess.run(
                ["docker", "rm", "--force", self.name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        )

    def _exists(self) -> bool:
        proc = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=^{self.name}$",
             "--format", "{{.Names}}"],
            stdout=subprocess.PIPE, text=True, check=False,
        )
        return self.name in proc.stdout.split()

    def _submit(self, req: LaunchRequest) -> None:
        self.broker.submit(sign_request(req, self.secret))

    def test_launch_creates_then_teardown_removes(self) -> None:
        self._submit(
            LaunchRequest(op="launch", bottle_id=self.bottle_id, image_ref=IMAGE)
        )
        self.assertTrue(self._exists(), "container should exist after launch")
        self._submit(LaunchRequest(op="teardown", bottle_id=self.bottle_id))
        self.assertFalse(self._exists(), "container should be gone after teardown")


if __name__ == "__main__":
    unittest.main()
