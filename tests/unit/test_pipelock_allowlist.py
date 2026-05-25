"""Unit: pipelock_effective_allowlist — the union of baked-in defaults,
bottle.egress.allowlist, and egress-proxy route hosts derived from
bottle.egress_proxy.routes (PRD 0017). Git upstreams declared in
bottle.git do not contribute here; they flow through the per-agent
git-gate (PRD 0008)."""

import unittest

from claude_bottle.manifest import Manifest
from claude_bottle.pipelock import (
    pipelock_effective_allowlist,
    pipelock_effective_tls_passthrough,
    pipelock_route_hosts,
)


def _bottle(spec):
    return Manifest.from_json_obj({
        "bottles": {"dev": spec},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }).bottles["dev"]


def _routes(routes):
    return {"egress_proxy": {"routes": routes}}


class TestEffectiveAllowlist(unittest.TestCase):
    def test_union_and_dedup(self):
        eff = pipelock_effective_allowlist(_bottle({
            "egress": {
                "allowlist": [
                    "registry.npmjs.org",
                    # Duplicate of a baked default; the union must dedupe.
                    "api.anthropic.com",
                ],
            },
        }))
        self.assertIn("api.anthropic.com", eff, "baked default present")
        self.assertIn("registry.npmjs.org", eff, "egress.allowlist present")
        self.assertEqual(len(eff), len(set(eff)), "deduplicated")
        self.assertEqual(eff, sorted(eff), "sorted")


class TestRouteHosts(unittest.TestCase):
    def test_each_route_contributes_its_host(self):
        hosts = pipelock_route_hosts(_bottle(_routes([
            {"host": "api.github.com",
             "auth": {"scheme": "Bearer", "token_ref": "GH"}},
            {"host": "github.com",
             "auth": {"scheme": "Bearer", "token_ref": "GH"}},
        ])))
        self.assertEqual(["api.github.com", "github.com"], hosts)

    def test_no_routes_empty(self):
        self.assertEqual([], pipelock_route_hosts(_bottle({})))


class TestAllowlistWithRoutes(unittest.TestCase):
    def test_route_hosts_added_to_allowlist(self):
        eff = pipelock_effective_allowlist(_bottle(_routes([
            {"host": "registry.npmjs.org",
             "auth": {"scheme": "Bearer", "token_ref": "N"}},
            {"host": "api.github.com",
             "auth": {"scheme": "Bearer", "token_ref": "G"}},
        ])))
        self.assertIn("registry.npmjs.org", eff)
        self.assertIn("api.github.com", eff)

    def test_egress_proxy_hostname_auto_added_when_routes_exist(self):
        # Egress-proxy's outbound leg uses HTTPS_PROXY=pipelock, so
        # any request that flows through egress-proxy → pipelock
        # would otherwise be rejected by pipelock's hostname gate.
        eff = pipelock_effective_allowlist(_bottle(_routes([
            {"host": "x.example",
             "auth": {"scheme": "Bearer", "token_ref": "T"}},
        ])))
        self.assertIn("egress-proxy", eff)

    def test_egress_proxy_hostname_NOT_added_when_no_routes(self):
        eff = pipelock_effective_allowlist(_bottle({}))
        self.assertNotIn("egress-proxy", eff)

    def test_supervise_hostname_auto_added_when_supervise_enabled(self):
        # The agent's MCP client opens long-polled requests to
        # http://supervise:9100/. They bypass the agent's HTTP_PROXY
        # (via NO_PROXY=supervise) and shouldn't traverse pipelock;
        # but for the launch path where supervise traffic does flow
        # through pipelock (egress-proxy → ... → supervise edge
        # cases), the hostname needs to be on the allowlist anyway.
        eff = pipelock_effective_allowlist(_bottle({"supervise": True}))
        self.assertIn("supervise", eff)

    def test_supervise_hostname_NOT_added_when_disabled(self):
        eff = pipelock_effective_allowlist(_bottle({}))
        self.assertNotIn("supervise", eff)
        eff_explicit = pipelock_effective_allowlist(_bottle({"supervise": False}))
        self.assertNotIn("supervise", eff_explicit)

    def test_path_allowlist_does_not_affect_pipelock_allowlist(self):
        # path_allowlist is enforced by egress-proxy, not pipelock.
        # Pipelock only sees the upstream hostname; the path filter
        # has already passed (or 403'd) at egress-proxy.
        eff = pipelock_effective_allowlist(_bottle(_routes([
            {"host": "github.com", "path_allowlist": ["/x/", "/y/"]},
        ])))
        self.assertIn("github.com", eff)
        # The path strings don't leak into the allowlist.
        for entry in eff:
            self.assertFalse(entry.startswith("/"))


class TestTlsPassthrough(unittest.TestCase):
    def test_default_includes_api_anthropic(self):
        passthrough = pipelock_effective_tls_passthrough(_bottle({}))
        self.assertEqual(["api.anthropic.com"], passthrough)

    def test_route_hosts_NOT_added_to_passthrough(self):
        # egress-proxy trusts pipelock's per-bottle CA, so pipelock
        # MITMs and body-scans the egress-proxy → upstream leg the
        # same way it scanned direct agent traffic before. Auto-adding
        # route hosts to passthrough would silently disable that.
        passthrough = pipelock_effective_tls_passthrough(_bottle(_routes([
            {"host": "api.github.com",
             "auth": {"scheme": "Bearer", "token_ref": "G"}},
            {"host": "registry.npmjs.org",
             "auth": {"scheme": "Bearer", "token_ref": "N"}},
        ])))
        self.assertEqual(["api.anthropic.com"], passthrough)


if __name__ == "__main__":
    unittest.main()
