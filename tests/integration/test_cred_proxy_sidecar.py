"""Integration: drive `DockerCredProxy.prepare` → `.start` against a
fake upstream container, then verify header injection / strip-and-
replace at the wire level (PRD 0010 SC2, SC3, SC6).

Topology mirrors production: a per-bottle internal docker network (no
default gateway) for the agent ↔ cred-proxy leg, and an egress network
for cred-proxy ↔ upstream. The "agent" is a curl container on the
internal net; the "upstream" is the fake-upstream container on the
egress net. cred-proxy straddles both.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from claude_bottle.backend.docker.cred_proxy import (
    CRED_PROXY_HOSTNAME,
    CRED_PROXY_PORT,
    DockerCredProxy,
    build_cred_proxy_image,
    cred_proxy_container_name,
)
from claude_bottle.backend.docker.network import (
    network_create_egress,
    network_create_internal,
    network_remove,
)
from claude_bottle.cred_proxy import CredProxy
from claude_bottle.manifest import Manifest
from tests._docker import skip_unless_docker


CURL_IMAGE = "curlimages/curl:latest"
FAKE_UPSTREAM_IMAGE = "python:3.13-alpine"
FAKE_UPSTREAM_HOST = "fake-upstream"
FAKE_UPSTREAM_PORT = "8080"


def _bottle(tokens):
    return Manifest.from_json_obj({
        "bottles": {"dev": {"tokens": tokens}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }).bottles["dev"]


class _StubCredProxy(CredProxy):
    """CredProxy.prepare's render uses the Kind defaults, but the
    integration test needs the cred-proxy to forward to the fake
    upstream — not api.anthropic.com / github.com / npmjs.org. We
    pass a one-route plan in directly via DockerCredProxy.start
    rather than going through the manifest path."""

    def start(self, plan): raise NotImplementedError
    def stop(self, target): return None


def _make_routes_json(upstream_host: str, upstream_port: str) -> str:
    payload = {
        "routes": [
            {
                "path": "/fake/",
                "upstream": f"http://{upstream_host}:{upstream_port}",
                "auth_scheme": "Bearer",
                "token_env": "CRED_PROXY_TOKEN_0",
            },
        ],
    }
    return json.dumps(payload, indent=2) + "\n"


@skip_unless_docker()
class TestCredProxySidecar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Pre-pull the probe + fake-upstream base images so per-test
        # retries don't race the registry. Skip if pulls fail (the
        # canary suite separately probes registry health).
        for image in (CURL_IMAGE, FAKE_UPSTREAM_IMAGE):
            r = subprocess.run(
                ["docker", "pull", image],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if r.returncode != 0:
                raise unittest.SkipTest(f"could not pull {image}")
        build_cred_proxy_image()

    def setUp(self):
        self.slug = f"cb-test-cp-{os.getpid()}"
        self.proxy_name = ""
        self.fake_name = f"fake-upstream-{self.slug}"
        self.internal_net = ""
        self.egress_net = ""
        self.work_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        for name in (self.proxy_name, self.fake_name):
            if name:
                subprocess.run(
                    ["docker", "rm", "-f", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
        for n in (self.internal_net, self.egress_net):
            if n:
                network_remove(n)
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _bring_up_fake_upstream(self) -> None:
        """Run the fake-upstream container on the egress network with
        the host stable name `fake-upstream`. Bind-mount the script
        from tests/integration/."""
        repo_dir = str(Path(__file__).resolve().parent.parent.parent)
        script = "tests/integration/_fake_upstream.py"
        r = subprocess.run(
            [
                "docker", "run", "-d",
                "--name", self.fake_name,
                "--hostname", FAKE_UPSTREAM_HOST,
                "--network", self.egress_net,
                "--network-alias", FAKE_UPSTREAM_HOST,
                "-v", f"{repo_dir}/{script}:/srv.py:ro",
                "-e", f"FAKE_UPSTREAM_PORT={FAKE_UPSTREAM_PORT}",
                FAKE_UPSTREAM_IMAGE,
                "python3", "/srv.py",
            ],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            self.fail(f"failed to start fake-upstream: {r.stderr}")

    def _start_cred_proxy_via_production_code(self) -> str:
        """Run DockerCredProxy.start with a plan that points at the
        fake upstream. We bypass the manifest path (which fixes
        upstreams by Kind) by handing .start an already-rendered
        routes.json."""
        from claude_bottle.cred_proxy import (
            CredProxyPlan,
            CredProxyUpstream,
        )
        routes_path = self.work_dir / "routes.json"
        routes_path.write_text(_make_routes_json(FAKE_UPSTREAM_HOST, FAKE_UPSTREAM_PORT))
        routes_path.chmod(0o600)
        plan = CredProxyPlan(
            slug=self.slug,
            routes_path=routes_path,
            upstreams=(CredProxyUpstream(
                kind="fake",
                path="/fake/",
                upstream=f"http://{FAKE_UPSTREAM_HOST}:{FAKE_UPSTREAM_PORT}",
                auth_scheme="Bearer",
                token_env="CRED_PROXY_TOKEN_0",
                token_ref="TEST_TOKEN",
            ),),
            token_env_map={"CRED_PROXY_TOKEN_0": "TEST_TOKEN"},
            internal_network=self.internal_net,
            egress_network=self.egress_net,
        )
        # Inject the host-side TEST_TOKEN into our process env so the
        # production resolver picks it up.
        os.environ["TEST_TOKEN"] = "real-token-injected-by-proxy"
        try:
            return DockerCredProxy().start(plan)
        finally:
            os.environ.pop("TEST_TOKEN", None)

    def _curl_via_internal_net(self, path: str, *extra: str) -> str:
        """Run a sibling curl container on the internal network — same
        access topology the agent uses in production — to hit the
        cred-proxy. Returns stdout."""
        r = subprocess.run(
            [
                "docker", "run", "--rm",
                "--network", self.internal_net,
                CURL_IMAGE,
                "-s", "--max-time", "10",
                "--retry", "20", "--retry-delay", "1", "--retry-connrefused",
                *extra,
                f"http://{CRED_PROXY_HOSTNAME}:{CRED_PROXY_PORT}{path}",
            ],
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(0, r.returncode,
                         f"curl failed: stdout={r.stdout!r} stderr={r.stderr!r}")
        return r.stdout

    def _query_fake_capture(self) -> dict:
        """Read the fake upstream's /__last_request endpoint to see
        what headers it received."""
        r = subprocess.run(
            [
                "docker", "run", "--rm",
                "--network", self.egress_net,
                CURL_IMAGE,
                "-s", "--max-time", "10",
                "--retry", "5", "--retry-delay", "1", "--retry-connrefused",
                f"http://{FAKE_UPSTREAM_HOST}:{FAKE_UPSTREAM_PORT}/__last_request",
            ],
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(0, r.returncode, f"capture query failed: {r.stderr}")
        return json.loads(r.stdout)

    @unittest.skipIf(
        os.environ.get("GITEA_ACTIONS") == "true",
        "skipped under act_runner: docker socket mount topology breaks "
        "in-process visibility of networks created on the host daemon",
    )
    def test_end_to_end_header_injection_and_strip(self):
        """Full bring-up via the production DockerCredProxy code path,
        then send a request from a sibling curl container with the
        agent's `Authorization` header. The fake upstream's capture
        must show:
          - the agent's Authorization was stripped (no `stolen` token)
          - the cred-proxy injected `Bearer real-token-injected-by-proxy`
          - the request reached the upstream at all
        """
        self.internal_net = network_create_internal(self.slug)
        self.egress_net = network_create_egress(self.slug)
        self._bring_up_fake_upstream()
        self.proxy_name = self._start_cred_proxy_via_production_code()
        self.assertEqual(cred_proxy_container_name(self.slug), self.proxy_name)

        # Agent → cred-proxy with a smuggled Authorization header.
        body = self._curl_via_internal_net(
            "/fake/v1/messages",
            "-H", "Authorization: Bearer stolen-by-prompt-injection",
            "-X", "POST",
            "-H", "Content-Type: application/json",
            "--data-binary", '{"hello":"world"}',
        )
        # The fake upstream responds with a fixed body.
        self.assertIn('"upstream":"fake"', body)

        # Now ask the fake upstream what headers it actually saw.
        captured = self._query_fake_capture()
        self.assertEqual("POST", captured["method"])
        self.assertEqual("/v1/messages", captured["path"],
                         "the /fake/ prefix should be stripped before forwarding")

        headers = {k.lower(): v for k, v in captured["headers"]}
        self.assertEqual(
            "Bearer real-token-injected-by-proxy",
            headers.get("authorization"),
            "cred-proxy must strip the inbound Authorization and inject "
            "the configured value",
        )
        self.assertNotIn("stolen", headers.get("authorization", ""),
                         "the agent's smuggled token must NOT reach upstream")
        self.assertEqual(
            FAKE_UPSTREAM_HOST,
            headers.get("host"),
            "Host header should point at the upstream, not the proxy",
        )

    @unittest.skipIf(
        os.environ.get("GITEA_ACTIONS") == "true",
        "skipped under act_runner: docker socket mount topology breaks "
        "in-process visibility of networks created on the host daemon",
    )
    def test_unknown_path_returns_404(self):
        """An agent reaching for an unconfigured route gets a 404,
        not a silent forward to anywhere."""
        self.internal_net = network_create_internal(self.slug)
        self.egress_net = network_create_egress(self.slug)
        self._bring_up_fake_upstream()
        self.proxy_name = self._start_cred_proxy_via_production_code()

        r = subprocess.run(
            [
                "docker", "run", "--rm",
                "--network", self.internal_net,
                CURL_IMAGE,
                "-s", "-o", "/dev/null", "-w", "%{http_code}",
                "--max-time", "10",
                "--retry", "20", "--retry-delay", "1", "--retry-connrefused",
                f"http://{CRED_PROXY_HOSTNAME}:{CRED_PROXY_PORT}/not-a-route",
            ],
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(0, r.returncode)
        self.assertEqual("404", r.stdout.strip())


if __name__ == "__main__":
    unittest.main()
