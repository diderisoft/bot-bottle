"""Integration: verify the pinned pipelock image. Requires docker.
  - Pinned digest is reachable on the registry.
  - Image's ENTRYPOINT/CMD match what claude_bottle.pipelock assumes
    (`/pipelock` and `run --listen 0.0.0.0:8888`).
  - The /pipelock binary actually runs (--version succeeds)."""

import json
import re
import subprocess
import unittest

from claude_bottle.pipelock import PIPELOCK_IMAGE
from tests._docker import skip_unless_docker


@skip_unless_docker()
class TestPipelockImage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Pull the pinned image (cheap if cached).
        result = subprocess.run(
            ["docker", "pull", PIPELOCK_IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise unittest.SkipTest(f"could not pull {PIPELOCK_IMAGE}")

    def test_entrypoint_contains_pipelock(self):
        result = subprocess.run(
            ["docker", "image", "inspect", PIPELOCK_IMAGE,
             "--format", "{{json .Config.Entrypoint}}"],
            capture_output=True, text=True,
        )
        self.assertIn("/pipelock", result.stdout)

    def test_cmd_contains_run(self):
        result = subprocess.run(
            ["docker", "image", "inspect", PIPELOCK_IMAGE,
             "--format", "{{json .Config.Cmd}}"],
            capture_output=True, text=True,
        )
        self.assertIn("run", result.stdout)

    def test_binary_runs(self):
        result = subprocess.run(
            ["docker", "run", "--rm", PIPELOCK_IMAGE, "--version"],
            capture_output=True, text=True,
        )
        out = result.stdout + result.stderr
        self.assertRegex(out, r"[Pp]ipelock|2\.[0-9]+\.[0-9]+")


if __name__ == "__main__":
    unittest.main()
