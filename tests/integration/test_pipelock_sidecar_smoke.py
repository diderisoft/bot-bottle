"""Integration: drive the production pipelock-sidecar bring-up
(`DockerPipelockProxy.prepare` → `.start`) and probe /health from a
sibling container on the same internal network. The point is that the
test exercises the production code path — if the docker create/cp/start
sequence in DockerPipelockProxy.start changes shape, this test should
notice.

We don't probe /health from the host because the sidecar is created
attached to an `--internal` network with no published port (that's
the production topology). An in-network curl container reaches it the
same way the agent container would in production.
"""

import dataclasses
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from claude_bottle.backend.docker.network import (
    network_create_egress,
    network_create_internal,
    network_remove,
)
from claude_bottle.backend.docker.pipelock import (
    PIPELOCK_PORT,
    DockerPipelockProxy,
    pipelock_container_name,
)
from tests._docker import skip_unless_docker
from tests.fixtures import fixture_minimal

CURL_IMAGE = "curlimages/curl:latest"


@skip_unless_docker()
class TestPipelockSidecarSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Pre-pull curlimages/curl so the per-test retry loop isn't
        # racing the registry. Skip cleanly if the pull fails (the
        # canary suite will surface a real registry outage separately).
        result = subprocess.run(
            ["docker", "pull", CURL_IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise unittest.SkipTest(f"could not pull {CURL_IMAGE}")

    def setUp(self):
        self.slug = f"cb-test-smoke-{os.getpid()}"
        self.sidecar_name = ""
        self.internal_net = ""
        self.egress_net = ""
        self.work_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        if self.sidecar_name:
            DockerPipelockProxy().stop(self.sidecar_name)
        for n in (self.internal_net, self.egress_net):
            if n:
                network_remove(n)
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def test_prepare_and_start_yield_healthy_sidecar(self):
        proxy = DockerPipelockProxy()

        prep = proxy.prepare(fixture_minimal().bottles["dev"], self.slug, self.work_dir)

        self.internal_net = network_create_internal(self.slug)
        self.egress_net = network_create_egress(self.slug)

        plan = dataclasses.replace(
            prep,
            internal_network=self.internal_net,
            egress_network=self.egress_net,
        )

        self.sidecar_name = proxy.start(plan)
        self.assertEqual(pipelock_container_name(self.slug), self.sidecar_name)

        # Probe /health from a sibling container on the internal network —
        # same access topology the agent container uses in production.
        # curl retries on connection refused while pipelock is booting.
        probe = subprocess.run(
            [
                "docker", "run", "--rm",
                "--network", self.internal_net,
                CURL_IMAGE,
                "-sf", "--max-time", "2",
                "--retry", "15",
                "--retry-delay", "1",
                "--retry-connrefused",
                f"http://{self.sidecar_name}:{PIPELOCK_PORT}/health",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            0, probe.returncode,
            f"health probe failed: stdout={probe.stdout!r} stderr={probe.stderr!r}",
        )
        body = probe.stdout
        self.assertIn('"status":"healthy"', body)
        self.assertRegex(body, r'"version":"[0-9]+\.[0-9]+\.[0-9]+"')


if __name__ == "__main__":
    unittest.main()
