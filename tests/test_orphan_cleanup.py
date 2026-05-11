"""Integration: the cleanup primitives the start-flow trap depends on
are idempotent. The original orphan-network bug was a trap-ordering
issue; the fix moved the install earlier. The trap is only safe if
network_remove and PipelockProxy.stop are no-ops against missing
resources."""

import os
import subprocess
import unittest

from claude_bottle.backend.docker.network import (
    network_create_egress,
    network_create_internal,
    network_remove,
)
from claude_bottle.backend.docker.pipelock import DockerPipelockProxy
from claude_bottle.pipelock import pipelock_container_name
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
            capture_output=True, text=True,
        ).stdout.splitlines()
        self.assertIn(self.internal_name, nets)
        self.assertIn(self.egress_name, nets)

        self.assertTrue(network_remove(self.internal_name))
        self.assertTrue(network_remove(self.egress_name))

        nets_after = subprocess.run(
            ["docker", "network", "ls", "--format", "{{.Name}}"],
            capture_output=True, text=True,
        ).stdout.splitlines()
        self.assertNotIn(self.internal_name, nets_after)
        self.assertNotIn(self.egress_name, nets_after)

        # Idempotent on already-removed.
        self.assertTrue(network_remove(self.internal_name))
        self.assertTrue(network_remove(self.egress_name))

    def test_pipelock_stop_missing_sidecar(self):
        # Should not raise.
        DockerPipelockProxy().stop(pipelock_container_name(f"missing-{self.slug}"))


if __name__ == "__main__":
    unittest.main()
