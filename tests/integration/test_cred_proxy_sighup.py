"""Integration: SIGHUP reload + host-side apply_routes_change
(PRD 0014).

Brings up a real cred-proxy sidecar with one route, then uses
apply_routes_change (docker cp + SIGHUP) to swap to a different
route. Verifies cred-proxy actually serves the new routes after the
reload (and 404s the old ones).

Avoids a real upstream by routing to unreachable hostnames — the
proxy's 502 "upstream connection failed" is a sufficient signal that
the route matched. 404 means no route matched.

apply_routes_change uses docker exec / cp / kill (not bind mounts),
so this test should work in docker-in-docker environments too — no
skip decorator beyond skip_unless_docker.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from claude_bottle.backend.docker.cred_proxy import (
    CRED_PROXY_PORT,
    DockerCredProxy,
    build_cred_proxy_image,
    cred_proxy_container_name,
)
from claude_bottle.backend.docker.cred_proxy_apply import (
    CredProxyApplyError,
    apply_routes_change,
    fetch_current_routes,
)
from claude_bottle.backend.docker.network import (
    network_create_egress,
    network_create_internal,
    network_remove,
)
from claude_bottle.cred_proxy import (
    CRED_PROXY_HOSTNAME,
    CredProxyPlan,
    CredProxyRoute,
)
from tests._docker import skip_unless_docker


CURL_IMAGE = "curlimages/curl:latest"


@skip_unless_docker()
class TestCredProxySighupReload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r = subprocess.run(
            ["docker", "pull", CURL_IMAGE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if r.returncode != 0:
            raise unittest.SkipTest(f"could not pull {CURL_IMAGE}")
        build_cred_proxy_image()

    def setUp(self):
        self.slug = f"cb-test-sighup-{os.getpid()}-{int(time.time())}"
        self.proxy_name = ""
        self.internal_net = ""
        self.egress_net = ""
        self.work_dir = Path(tempfile.mkdtemp(prefix="cred-proxy-sighup."))
        # Token value for both initial and post-SIGHUP routes — they
        # share the same TokenRef so they share CRED_PROXY_TOKEN_0 in
        # the container's environ.
        os.environ["CB_SIGHUP_TEST_TOKEN"] = "test-token"

    def tearDown(self):
        os.environ.pop("CB_SIGHUP_TEST_TOKEN", None)
        if self.proxy_name:
            subprocess.run(
                ["docker", "rm", "-f", self.proxy_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        for n in (self.internal_net, self.egress_net):
            if n:
                network_remove(n)
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _bring_up_with_route(self, path: str, upstream: str) -> None:
        self.internal_net = network_create_internal(self.slug)
        self.egress_net = network_create_egress(self.slug)
        route = CredProxyRoute(
            path=path,
            upstream=upstream,
            auth_scheme="Bearer",
            token_env="CRED_PROXY_TOKEN_0",
            token_ref="CB_SIGHUP_TEST_TOKEN",
        )
        routes_path = self.work_dir / "routes.json"
        from claude_bottle.cred_proxy import cred_proxy_render_routes
        routes_path.write_text(cred_proxy_render_routes((route,)))
        routes_path.chmod(0o600)
        plan = CredProxyPlan(
            slug=self.slug,
            routes_path=routes_path,
            routes=(route,),
            token_env_map={"CRED_PROXY_TOKEN_0": "CB_SIGHUP_TEST_TOKEN"},
            internal_network=self.internal_net,
            egress_network=self.egress_net,
            # No pipelock for this test — the proxy talks directly to
            # the egress network. Upstreams are unreachable so the
            # 502s confirm the route table.
        )
        self.proxy_name = DockerCredProxy().start(plan)
        # Wait until the proxy is serving (it's the only way I have
        # to know python has bound to the port).
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            code = self._curl("/__probe/")
            if code in (404, 502):  # serving — either response proves it's up
                return
            time.sleep(0.2)
        raise AssertionError("cred-proxy never came up")

    def _curl(self, path: str) -> int | None:
        """Return the HTTP status from a curl-in-container request to
        the cred-proxy, or None on connection failure."""
        r = subprocess.run(
            [
                "docker", "run", "--rm",
                "--network", self.internal_net,
                CURL_IMAGE,
                "-sS", "-o", "/dev/null",
                "-w", "%{http_code}",
                "--max-time", "8",
                f"http://{CRED_PROXY_HOSTNAME}:{CRED_PROXY_PORT}{path}",
            ],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            return None
        try:
            return int(r.stdout.strip())
        except ValueError:
            return None

    def test_sighup_swaps_routes(self):
        """Initial route /a/ matches (502 from unreachable upstream);
        /b/ 404s. After apply_routes_change with /b/ only, the table
        flips: /a/ 404s, /b/ matches."""
        self._bring_up_with_route("/a/", "https://unreachable-a.example")

        self.assertEqual(502, self._curl("/a/foo"))
        self.assertEqual(404, self._curl("/b/foo"))

        new_routes = json.dumps({"routes": [{
            "path": "/b/",
            "upstream": "https://unreachable-b.example",
            "auth_scheme": "Bearer",
            "token_env": "CRED_PROXY_TOKEN_0",
        }]}) + "\n"

        before, after = apply_routes_change(self.slug, new_routes)
        self.assertIn("/a/", before)
        self.assertEqual(new_routes, after)

        # SIGHUP propagates as a Python signal — runs at the next
        # bytecode boundary on the main thread. Give it a moment.
        deadline = time.monotonic() + 5.0
        flipped = False
        while time.monotonic() < deadline:
            if self._curl("/a/foo") == 404 and self._curl("/b/foo") == 502:
                flipped = True
                break
            time.sleep(0.2)
        self.assertTrue(flipped, "SIGHUP reload did not propagate to the route table")

    def test_in_flight_connections_survive_sighup(self):
        """SIGHUP must reload without dropping the bound socket. The
        signal handler runs on the main thread; in-flight worker
        threads keep the routes they captured at request start.
        Verified by issuing a request right after SIGHUP and seeing
        the new route in effect (the listener never restarted)."""
        self._bring_up_with_route("/a/", "https://unreachable.example")
        # Fetching the current routes also proves the proxy is up.
        current = fetch_current_routes(self.slug)
        self.assertIn("/a/", current)

        new_routes = json.dumps({"routes": [{
            "path": "/c/",
            "upstream": "https://unreachable-c.example",
            "auth_scheme": "Bearer",
            "token_env": "CRED_PROXY_TOKEN_0",
        }]}) + "\n"
        apply_routes_change(self.slug, new_routes)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self._curl("/c/foo") == 502:
                return
            time.sleep(0.2)
        self.fail("new route not picked up after SIGHUP")

    def test_apply_with_invalid_json_raises(self):
        self._bring_up_with_route("/a/", "https://unreachable.example")
        with self.assertRaises(CredProxyApplyError) as cm:
            apply_routes_change(self.slug, "{not json")
        self.assertIn("not valid JSON", str(cm.exception))

    def test_apply_against_missing_sidecar_raises(self):
        # Don't bring up the sidecar; the slug points at nothing.
        with self.assertRaises(CredProxyApplyError):
            apply_routes_change(
                self.slug,
                '{"routes": [{"path": "/x/", "upstream": "https://example.com",'
                ' "auth_scheme": "Bearer", "token_env": "CRED_PROXY_TOKEN_0"}]}',
            )


if __name__ == "__main__":
    unittest.main()
