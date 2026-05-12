"""Canary: the pinned pipelock image's binary actually runs.

This test exists to catch a broken upstream packaging at the pinned
digest. It is NOT part of the per-push suite — that would couple every
dev push to upstream registry availability. Set
CLAUDE_BOTTLE_RUN_CANARIES=1 to opt in (a scheduled CI workflow does
this; humans can run it ad-hoc the same way).
"""

import os
import subprocess
import unittest

from claude_bottle.backend.docker.pipelock import PIPELOCK_IMAGE
from tests._docker import skip_unless_docker


@unittest.skipUnless(
    os.environ.get("CLAUDE_BOTTLE_RUN_CANARIES") == "1",
    "canary suite is opt-in; set CLAUDE_BOTTLE_RUN_CANARIES=1 to run",
)
@skip_unless_docker()
class TestPipelockImage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            ["docker", "pull", PIPELOCK_IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            raise unittest.SkipTest(f"could not pull {PIPELOCK_IMAGE}")

    def test_binary_runs(self):
        result = subprocess.run(
            ["docker", "run", "--rm", PIPELOCK_IMAGE, "--version"],
            capture_output=True, text=True, check=False,
        )
        out = result.stdout + result.stderr
        self.assertRegex(out, r"[Pp]ipelock|2\.[0-9]+\.[0-9]+")


if __name__ == "__main__":
    unittest.main()
