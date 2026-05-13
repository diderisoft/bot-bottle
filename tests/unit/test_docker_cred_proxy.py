"""Unit: DockerCredProxy helpers + early-exit guards (PRD 0010).

The full docker lifecycle is exercised by integration tests; here we
cover the pure helpers and the validation checks `.start` runs
before touching docker."""

import unittest
from pathlib import Path

from claude_bottle.backend.docker.cred_proxy import (
    CRED_PROXY_HOSTNAME,
    CRED_PROXY_PORT,
    DockerCredProxy,
    cred_proxy_container_name,
    cred_proxy_url,
)
from claude_bottle.cred_proxy import CredProxyPlan, CredProxyUpstream
from claude_bottle.log import Die


def _empty_plan(**overrides):
    base = {
        "slug": "demo",
        "routes_path": Path("/nonexistent"),
        "upstreams": (),
        "token_env_map": {},
        "internal_network": "",
        "egress_network": "",
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
        upstream = CredProxyUpstream(
            kind="anthropic", path="/anthropic/",
            upstream="https://api.anthropic.com",
            auth_scheme="Bearer", token_env="CRED_PROXY_TOKEN_0",
            token_ref="T",
        )
        with self.assertRaises(Die):
            self.proxy.start(_empty_plan(upstreams=(upstream,)))

    def test_missing_routes_file_dies(self):
        upstream = CredProxyUpstream(
            kind="anthropic", path="/anthropic/",
            upstream="https://api.anthropic.com",
            auth_scheme="Bearer", token_env="CRED_PROXY_TOKEN_0",
            token_ref="T",
        )
        with self.assertRaises(Die):
            self.proxy.start(_empty_plan(
                upstreams=(upstream,),
                internal_network="net-x",
                egress_network="egress-x",
                routes_path=Path("/tmp/cred-proxy-test-does-not-exist.json"),
            ))


if __name__ == "__main__":
    unittest.main()
