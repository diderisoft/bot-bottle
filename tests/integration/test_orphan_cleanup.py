"""Integration: the network-cleanup primitives the start-flow trap
depends on are idempotent. The original orphan-network bug was a
trap-ordering issue; the fix moved the install earlier. The trap
is only safe if network_remove is a no-op against missing
resources.

The PipelockProxy.stop idempotency case that used to live here was
removed in PRD 0024 chunk 3 when the per-container .stop method
went away — sidecar teardown is now compose's responsibility, and
`compose down` already no-ops on missing containers."""

import os
import subprocess
import unittest

from claude_bottle.backend.docker.network import (
    network_create_egress,
    network_create_internal,
    network_remove,
)
from tests._docker import skip_unless_docker


@skip_unless_docker()
class TestOrphanCleanup(unittest.TestCase):
    def setUp(self):
        self.slug = f"cb-test-orphan-{os.getpid()}"
        self.internal_name = ""
        self.egress_name = ""

    def tearDown(self):
        for n in (self.internal_name, self.egress_name):
            if n:
                subprocess.run(
                    ["docker", "network", "rm", n],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )

    def test_remove_missing_is_noop(self):
        # Returning True == idempotent success.
        self.assertTrue(network_remove(f"claude-bottle-net-{self.slug}-does-not-exist"))

    @unittest.skipIf(
        os.environ.get("GITEA_ACTIONS") == "true",
        "skipped under act_runner: docker socket mount topology breaks "
        "in-process visibility of networks created on the host daemon",
    )
    def test_create_and_remove(self):
        self.internal_name = network_create_internal(self.slug)
        self.egress_name = network_create_egress(self.slug)

        nets = subprocess.run(
            ["docker", "network", "ls", "--format", "{{.Name}}"],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        self.assertIn(self.internal_name, nets)
        self.assertIn(self.egress_name, nets)

        self.assertTrue(network_remove(self.internal_name))
        self.assertTrue(network_remove(self.egress_name))

        nets_after = subprocess.run(
            ["docker", "network", "ls", "--format", "{{.Name}}"],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        self.assertNotIn(self.internal_name, nets_after)
        self.assertNotIn(self.egress_name, nets_after)

        # Idempotent on already-removed.
        self.assertTrue(network_remove(self.internal_name))
        self.assertTrue(network_remove(self.egress_name))


if __name__ == "__main__":
    unittest.main()
