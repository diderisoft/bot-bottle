"""Integration: DockerGateway.image_exists reflects real docker state.

Gated on a reachable Docker daemon. Deliberately does NOT build the full
gateway (a heavy, slow image build) — it exercises the `image_exists`
primitive that `ensure_built` gates on, against a tiny pulled image and a
name that can't exist.
"""

from __future__ import annotations

import subprocess
import unittest

from bot_bottle.backend.docker.gateway import DockerGateway
from tests._docker import skip_unless_docker

IMAGE = "busybox"


@skip_unless_docker()
class TestDockerGatewayImageExists(unittest.TestCase):
    def test_image_exists_true_for_present_false_for_absent(self) -> None:
        # Ensure the tiny image is present (build_if_missing is disabled here
        # so this never triggers a Dockerfile.gateway build).
        subprocess.run(
            ["docker", "pull", IMAGE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        self.assertTrue(DockerGateway(IMAGE, dockerfile=None).image_exists())
        self.assertFalse(
            DockerGateway("bot-bottle-nonexistent:doesnotexist", dockerfile=None).image_exists()
        )


if __name__ == "__main__":
    unittest.main()
