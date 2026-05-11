"""Integration: the pinned pipelock image's binary actually runs.
Catches a broken upstream packaging at the pinned digest. Requires
docker."""

import subprocess
import unittest

from claude_bottle.pipelock import PIPELOCK_IMAGE
from tests._docker import skip_unless_docker


@skip_unless_docker()
class TestPipelockImage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            ["docker", "pull", PIPELOCK_IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise unittest.SkipTest(f"could not pull {PIPELOCK_IMAGE}")

    def test_binary_runs(self):
        result = subprocess.run(
            ["docker", "run", "--rm", PIPELOCK_IMAGE, "--version"],
            capture_output=True, text=True,
        )
        out = result.stdout + result.stderr
        self.assertRegex(out, r"[Pp]ipelock|2\.[0-9]+\.[0-9]+")


if __name__ == "__main__":
    unittest.main()
