"""Integration: macOS Container launch topology.

End-to-end against Apple's real `container` runtime. The smoke launches
a bottle with the experimental macOS Container backend and verifies the
properties that make the explicit-proxy launch acceptable:

  - the agent can exec commands after provisioning;
  - HTTP(S)_PROXY points at the sidecar's internal-network IP;
  - allowlisted HTTPS reaches the egress sidecar;
  - direct egress with proxy env removed fails from the internal-only
    agent network;
  - non-allowlisted proxy traffic is blocked.

Skipped under Gitea Actions and on hosts without Apple's `container`.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from bot_bottle.backend import BottleSpec, get_bottle_backend
from bot_bottle.backend.macos_container.util import (
    dns_server as _container_dns_server,
    is_available as _container_available,
)
from bot_bottle.manifest import Manifest


_AGENT_PROMPT = "You are a launch smoke-test agent. Be brief."


def _minimal_agent_dockerfile(path: Path) -> None:
    path.write_text(
        "\n".join((
            "FROM node:22-slim",
            "RUN apt-get update \\",
            "  && apt-get install -y --no-install-recommends \\",
            "    ca-certificates curl git \\",
            "  && rm -rf /var/lib/apt/lists/*",
            "USER node",
            "WORKDIR /home/node",
            "CMD [\"sleep\", \"infinity\"]",
            "",
        )),
        encoding="utf-8",
    )


def _minimal_manifest(dockerfile: Path) -> Manifest:
    return Manifest.from_json_obj({
        "bottles": {
            "dev": {
                "agent_provider": {
                    "template": "pi",
                    "dockerfile": str(dockerfile),
                    "settings": {
                        "provider": "example",
                        "base_url": "https://example.com/v1",
                        "models": ["smoke"],
                    },
                },
                "egress": {
                    "routes": [
                        {"host": "example.com"},
                    ],
                },
            },
        },
        "agents": {
            "demo": {
                "skills": [],
                "prompt": _AGENT_PROMPT,
                "bottle": "dev",
            },
        },
    })


def _buildkit_dns_available() -> bool:
    if platform.system() != "Darwin" or not _container_available():
        return False
    stage = Path(tempfile.mkdtemp(prefix="cb-container-buildkit-dns."))
    image = "bot-bottle-buildkit-dns-check:latest"
    try:
        dockerfile = stage / "Dockerfile"
        dockerfile.write_text(
            "FROM debian:bookworm-slim\n"
            "RUN getent hosts deb.debian.org\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                "container", "build",
                "--dns", _container_dns_server(),
                "-t", image,
                "-f", str(dockerfile),
                str(stage),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    finally:
        subprocess.run(
            ["container", "image", "delete", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        shutil.rmtree(stage, ignore_errors=True)


@unittest.skipIf(
    os.environ.get("GITEA_ACTIONS") == "true",
    "skipped under act_runner: cannot host Apple Container VMs",
)
@unittest.skipUnless(
    platform.system() == "Darwin",
    "Apple Container is macOS-only",
)
@unittest.skipUnless(
    _container_available(),
    "Apple Container not on PATH; install from "
    "https://github.com/apple/container/releases",
)
@unittest.skipUnless(
    _buildkit_dns_available(),
    "Apple Container BuildKit cannot resolve deb.debian.org on this host",
)
class TestMacosContainerLaunch(unittest.TestCase):
    """Launch once and reuse the bottle across probes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.stage = Path(tempfile.mkdtemp(prefix="cb-macos-container-launch."))
        cls._launch = None
        cls.bottle = None
        dockerfile = cls.stage / "Dockerfile.agent-smoke"
        _minimal_agent_dockerfile(dockerfile)
        os.environ["BOT_BOTTLE_BACKEND"] = "macos-container"
        try:
            backend = get_bottle_backend()
            spec = BottleSpec(
                manifest=_minimal_manifest(dockerfile),
                agent_name="demo",
                copy_cwd=False,
                user_cwd=str(cls.stage),
            )
            cls.plan = backend.prepare(spec, stage_dir=cls.stage)
            cls._launch = backend.launch(cls.plan)
            cls.bottle = cls._launch.__enter__()
        except BaseException:
            if cls._launch is not None:
                cls._launch.__exit__(None, None, None)
            shutil.rmtree(cls.stage, ignore_errors=True)
            os.environ.pop("BOT_BOTTLE_BACKEND", None)
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            if cls._launch is not None:
                cls._launch.__exit__(None, None, None)
        finally:
            shutil.rmtree(cls.stage, ignore_errors=True)
            os.environ.pop("BOT_BOTTLE_BACKEND", None)

    def test_smoke_exec_echo(self):
        r = self.bottle.exec(  # type: ignore[union-attr]
            "echo hello-from-macos-container"
        )
        self.assertEqual(0, r.returncode, msg=r.stderr)
        self.assertIn("hello-from-macos-container", r.stdout)

    def test_proxy_env_points_at_sidecar_internal_ip(self):
        r = self.bottle.exec(  # type: ignore[union-attr]
            "printf '%s\n' \"$HTTPS_PROXY\" \"$HTTP_PROXY\" "
            "\"$NO_PROXY\" \"$NODE_EXTRA_CA_CERTS\""
        )
        self.assertEqual(0, r.returncode, msg=r.stderr)
        values = [line.strip() for line in r.stdout.splitlines()]
        self.assertEqual(4, len(values), values)
        self.assertEqual(values[0], values[1], values)
        self.assertRegex(values[0], r"^http://[0-9.]+:9099$")
        self.assertNotIn("127.0.0.1", values[0])
        sidecar_host = values[0].removeprefix("http://").removesuffix(":9099")
        self.assertIn(sidecar_host, values[2])
        self.assertEqual(
            "/usr/local/share/ca-certificates/bot-bottle-mitm-ca.crt",
            values[3],
        )

    def test_allowlisted_https_reaches_egress_proxy(self):
        r = self.bottle.exec(  # type: ignore[union-attr]
            "curl -fsS --max-time 20 https://example.com >/dev/null && echo OK"
        )
        self.assertEqual(0, r.returncode, msg=r.stderr + r.stdout)
        self.assertIn("OK", r.stdout)

    def test_direct_egress_bypass_without_proxy_fails(self):
        r = self.bottle.exec(  # type: ignore[union-attr]
            "env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy "
            "curl -s --show-error --max-time 5 https://example.com 2>&1 || true"
        )
        self.assertTrue(
            "refused" in r.stdout.lower()
            or "timed out" in r.stdout.lower()
            or "unreachable" in r.stdout.lower()
            or "failed" in r.stdout.lower()
            or "could not resolve" in r.stdout.lower()
            or "connection reset" in r.stdout.lower(),
            f"expected direct egress to fail; got: {r.stdout!r}",
        )

    def test_non_allowlisted_host_fails_through_proxy(self):
        r = self.bottle.exec(  # type: ignore[union-attr]
            "curl -s --show-error --max-time 10 https://iana.org 2>&1 || true"
        )
        self.assertTrue(
            "403" in r.stdout
            or "502" in r.stdout
            or "blocked" in r.stdout.lower()
            or "not allowed" in r.stdout.lower()
            or "not in the bottle's egress.routes allowlist" in r.stdout.lower()
            or "forbidden" in r.stdout.lower()
            or "failed" in r.stdout.lower(),
            f"expected non-allowlisted proxy request to fail; got: {r.stdout!r}",
        )


if __name__ == "__main__":
    unittest.main()
