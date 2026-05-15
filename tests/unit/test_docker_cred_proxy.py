"""Unit: DockerCredProxy helpers + early-exit guards (PRD 0010).

The full docker lifecycle is exercised by integration tests; here we
cover the pure helpers and the validation checks `.start` runs
before touching docker."""

import tempfile
import unittest
from pathlib import Path

from claude_bottle.backend.docker.cred_proxy import (
    CRED_PROXY_HOSTNAME,
    CRED_PROXY_PORT,
    DockerCredProxy,
    cred_proxy_container_name,
    cred_proxy_url,
)
from claude_bottle.cred_proxy import CredProxyPlan, CredProxyRoute
from claude_bottle.log import Die


def _empty_plan(**overrides):
    base = {
        "slug": "demo",
        "routes_path": Path("/nonexistent"),
        "routes": (),
        "token_env_map": {},
        "internal_network": "",
        "egress_network": "",
        "pipelock_ca_host_path": Path(),
        "pipelock_proxy_url": "",
    }
    base.update(overrides)
    return CredProxyPlan(**base)


class TestNameAndUrl(unittest.TestCase):
    def test_container_name_carries_slug(self):
        self.assertEqual("claude-bottle-cred-proxy-demo",
                         cred_proxy_container_name("demo"))

    def test_url_uses_alias_not_container_name(self):
        # The URL agents dial is stable across bottles — the slug
        # never appears in it. That's the whole point of attaching
        # --network-alias cred-proxy on the internal network.
        self.assertEqual(f"http://{CRED_PROXY_HOSTNAME}:{CRED_PROXY_PORT}",
                         cred_proxy_url())


class TestStartGuards(unittest.TestCase):
    def setUp(self):
        self.proxy = DockerCredProxy()

    def test_empty_upstreams_dies(self):
        with self.assertRaises(Die):
            self.proxy.start(_empty_plan())

    def test_missing_internal_network_dies(self):
        upstream = CredProxyRoute(
            path="/anthropic/",
            upstream="https://api.anthropic.com",
            auth_scheme="Bearer", token_env="CRED_PROXY_TOKEN_0",
            token_ref="T",
        )
        with self.assertRaises(Die):
            self.proxy.start(_empty_plan(routes=(upstream,)))

    def test_missing_routes_file_dies(self):
        upstream = CredProxyRoute(
            path="/anthropic/",
            upstream="https://api.anthropic.com",
            auth_scheme="Bearer", token_env="CRED_PROXY_TOKEN_0",
            token_ref="T",
        )
        with self.assertRaises(Die):
            self.proxy.start(_empty_plan(
                routes=(upstream,),
                internal_network="net-x",
                egress_network="egress-x",
                routes_path=Path("/tmp/cred-proxy-test-does-not-exist.json"),
            ))

    def test_pipelock_url_without_ca_dies(self):
        # URL set + CA path empty/missing is a wiring bug: either both
        # populated (production) or both empty (test escape hatch).
        upstream = CredProxyRoute(
            path="/anthropic/",
            upstream="https://api.anthropic.com",
            auth_scheme="Bearer", token_env="CRED_PROXY_TOKEN_0",
            token_ref="T",
        )
        with tempfile.NamedTemporaryFile() as routes:
            with self.assertRaises(Die):
                self.proxy.start(_empty_plan(
                    routes=(upstream,),
                    internal_network="net-x",
                    egress_network="egress-x",
                    routes_path=Path(routes.name),
                    pipelock_proxy_url="http://pipelock:8888",
                    pipelock_ca_host_path=Path("/tmp/cred-proxy-no-ca.pem"),
                ))


if __name__ == "__main__":
    unittest.main()
