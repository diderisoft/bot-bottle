"""Integration: full sidecar smoke test. Boots a pipelock container the
same way cli.py does (docker create + docker cp YAML + docker start),
then probes /health."""

import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from claude_bottle.backend.docker.pipelock import (
    PIPELOCK_IMAGE,
    DockerPipelockProxy,
)
from tests._docker import skip_unless_docker
from tests.fixtures import fixture_minimal


@skip_unless_docker()
class TestPipelockSidecarSmoke(unittest.TestCase):
    def setUp(self):
        self.name = f"cb-test-pipelock-smoke-{os.getpid()}"
        self.work_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        subprocess.run(
            ["docker", "rm", "-f", self.name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        shutil.rmtree(self.work_dir, ignore_errors=True)

    @unittest.skipIf(
        os.environ.get("GITEA_ACTIONS") == "true",
        "skipped under act_runner: published port is on the host's "
        "loopback, not reachable from the job container's 127.0.0.1",
    )
    def test_smoke(self):
        yaml_path = self.work_dir / "pipelock.yaml"
        DockerPipelockProxy().prepare(fixture_minimal(), "dev", "demo", yaml_path)

        create = subprocess.run(
            [
                "docker", "create",
                "--name", self.name,
                "-p", "0:8888",
                PIPELOCK_IMAGE,
                "run", "--config", "/etc/pipelock.yaml",
                "--listen", "0.0.0.0:8888",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(0, create.returncode, f"docker create failed: {create.stderr}")

        # Guard against /etc/pipelock/ regressions: the path must be
        # /etc/pipelock.yaml, since the image is distroless.
        cp = subprocess.run(
            ["docker", "cp", str(yaml_path), f"{self.name}:/etc/pipelock.yaml"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(0, cp.returncode, f"docker cp failed: {cp.stderr}")

        start = subprocess.run(
            ["docker", "start", self.name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(0, start.returncode,
                         f"docker start failed; check argv 'run --listen 0.0.0.0:8888'")

        port_result = subprocess.run(
            ["docker", "port", self.name, "8888"],
            capture_output=True, text=True,
        )
        first_line = (port_result.stdout or "").splitlines()[0] if port_result.stdout else ""
        host_port = first_line.rsplit(":", 1)[-1] if first_line else ""
        self.assertTrue(host_port, "could not determine published port")

        health_url = f"http://127.0.0.1:{host_port}/health"
        body = ""
        for _ in range(15):
            try:
                with urllib.request.urlopen(health_url, timeout=2) as resp:
                    body = resp.read().decode("utf-8")
                    break
            except (urllib.error.URLError, urllib.error.HTTPError, ConnectionError):
                time.sleep(1)

        self.assertIn('"status":"healthy"', body, "health body status:healthy")
        self.assertRegex(body, r'"version":"[0-9]+\.[0-9]+\.[0-9]+"',
                         "health body has version field")


if __name__ == "__main__":
    unittest.main()
