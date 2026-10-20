"""Integration: PRD 0024 chunk 1 — the gateway image builds
and the daemon binaries are present + executable inside it.

This test does NOT exercise the daemons running against real
config (routes.yaml, etc) — that lands in chunk 2 when the
renderer wires the bundle into compose. What we verify here is
the chunk-1 contract:

  - Dockerfile.gateway builds (multi-stage works, base layers
    pull, COPYs resolve).
  - gitleaks, mitmdump are at the documented paths and answer
    `--version`.
  - The Python init at /app/gateway_init.py runs and prints the
    expected "no daemons selected" line when the supervisor is
    pointed at an empty daemon set.

Skips cleanly only when the selected Docker backend is unavailable.
"""

from __future__ import annotations

import os
import subprocess
import unittest

from bot_bottle import resources
from tests._backend import skip_unless_backend


_IMAGE = "bot-bottle-gateway-test:chunk1"
_DOCKERFILE = "Dockerfile.gateway"


@skip_unless_backend("docker")
class TestGatewayImage(unittest.TestCase):
    """Builds the image once for the class, then runs a few
    `docker run` probes against it."""

    @classmethod
    def setUpClass(cls) -> None:
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        build_args = resources.image_build_args(
            _DOCKERFILE,
            context=repo_root,
        )
        arg_flags = [
            item
            for name, value in build_args.items()
            for item in ("--build-arg", f"{name}={value}")
        ]
        proc = subprocess.run(
            ["docker", "build", "-t", _IMAGE,
             "-f", _DOCKERFILE, *arg_flags, "."],
            cwd=repo_root,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"docker build failed; image probes cannot run.\n"
                f"{proc.stdout.decode('utf-8', errors='replace')[-2000:]}"
            )

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(
            ["docker", "image", "rm", "-f", _IMAGE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        )

    def _run_in_image(self, *cmd: str, timeout: float = 30.0) -> tuple[int, str]:
        proc = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", cmd[0], _IMAGE,
            *cmd[1:]],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.decode("utf-8", errors="replace")

    def test_gitleaks_binary_present_and_versioned(self):
        rc, out = self._run_in_image("/usr/bin/gitleaks", "version")
        self.assertEqual(0, rc, msg=out)
        # gitleaks prints a bare version string like "v8.x.y".
        self.assertRegex(out, r"v?\d+\.\d+")

    def test_mitmdump_binary_present_and_versioned(self):
        rc, out = self._run_in_image("mitmdump", "--version")
        self.assertEqual(0, rc, msg=out)
        self.assertIn("Mitmproxy", out)

    def test_python_imports_supervise_module(self):
        # The supervise daemon is installed as part of the bot_bottle
        # package (5ad3449), not as flat sibling modules under /app.
        # Probe that the package imports resolve inside the image.
        rc, out = self._run_in_image(
            "python3", "-c",
            "from bot_bottle.contract import supervise; "
            "from bot_bottle.gateway.supervisor import server as supervise_server; "
            "print('ok')",
        )
        self.assertEqual(0, rc, msg=out)
        self.assertIn("ok", out)

    def test_init_supervisor_runs_with_no_daemons(self):
        # `nothing` matches no canonical daemon → supervisor exits 0
        # immediately with the documented message. Confirms the
        # ENTRYPOINT wiring works.
        proc = subprocess.run(
            ["docker", "run", "--rm",
             "-e", "BOT_BOTTLE_GATEWAY_DAEMONS=nothing",
             _IMAGE],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=10.0,
            check=False,
        )
        out = proc.stdout.decode("utf-8", errors="replace")
        self.assertEqual(0, proc.returncode, msg=out)
        self.assertIn("no daemons selected", out)


if __name__ == "__main__":
    unittest.main()
