"""Unit: pipelock_effective_allowlist — the union of baked-in defaults,
bottle.egress.allowlist, and cred-proxy upstream hosts derived from
bottle.cred_proxy.routes (PRD 0010). Git upstreams declared in bottle.git
do not contribute here; they flow through the per-agent git-gate (PRD 0008)."""

import unittest

from claude_bottle.manifest import Manifest
from claude_bottle.pipelock import (
    pipelock_effective_allowlist,
    pipelock_effective_tls_passthrough,
    pipelock_token_hosts,
)


def _bottle(spec):
    return Manifest.from_json_obj({
        "bottles": {"dev": spec},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }).bottles["dev"]


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


def _routes(routes):
    return {"cred_proxy": {"routes": routes}}


class TestTokenHosts(unittest.TestCase):
    def test_each_route_contributes_its_upstream_host(self):
        hosts = pipelock_token_hosts(_bottle(_routes([
            {"path": "/gh-api/", "upstream": "https://api.github.com",
             "auth_scheme": "Bearer", "token_ref": "GH"},
            {"path": "/gh-git/", "upstream": "https://github.com",
             "auth_scheme": "Bearer", "token_ref": "GH"},
        ])))
        self.assertEqual(["api.github.com", "github.com"], hosts)

    def test_dedupe_across_routes(self):
        hosts = pipelock_token_hosts(_bottle(_routes([
            {"path": "/a/", "upstream": "https://x.example",
             "auth_scheme": "Bearer", "token_ref": "T1"},
            {"path": "/b/", "upstream": "https://x.example",
             "auth_scheme": "Bearer", "token_ref": "T2"},
        ])))
        self.assertEqual(["x.example"], hosts)

    def test_no_routes_empty(self):
        self.assertEqual([], pipelock_token_hosts(_bottle({})))


class TestAllowlistWithTokens(unittest.TestCase):
    def test_route_hosts_added_to_allowlist(self):
        eff = pipelock_effective_allowlist(_bottle(_routes([
            {"path": "/npm/", "upstream": "https://registry.npmjs.org",
             "auth_scheme": "Bearer", "token_ref": "N"},
            {"path": "/gh-api/", "upstream": "https://api.github.com",
             "auth_scheme": "Bearer", "token_ref": "G"},
        ])))
        self.assertIn("registry.npmjs.org", eff)
        self.assertIn("api.github.com", eff)

    def test_cred_proxy_hostname_auto_added_when_routes_exist(self):
        # The agent's HTTP_PROXY points at pipelock, so a request for
        # http://cred-proxy:9099/... arrives at pipelock as a request
        # for hostname `cred-proxy`. pipelock must allow it or the
        # agent can't reach its own sidecar.
        eff = pipelock_effective_allowlist(_bottle(_routes([
            {"path": "/x/", "upstream": "https://x.example",
             "auth_scheme": "Bearer", "token_ref": "T"},
        ])))
        self.assertIn("cred-proxy", eff)

    def test_cred_proxy_hostname_NOT_added_when_no_routes(self):
        # No cred-proxy sidecar, no auto-allow.
        eff = pipelock_effective_allowlist(_bottle({}))
        self.assertNotIn("cred-proxy", eff)

    def test_supervise_hostname_auto_added_when_supervise_enabled(self):
        # Same reasoning as cred-proxy: the agent's HTTP_PROXY points
        # at pipelock, so http://supervise:9100/ (the MCP endpoint)
        # arrives at pipelock as hostname `supervise`. Without this
        # auto-allow, claude-code's MCP client gets a 403 and the
        # supervise server shows up as "failed" in /mcp.
        eff = pipelock_effective_allowlist(_bottle({"supervise": True}))
        self.assertIn("supervise", eff)

    def test_supervise_hostname_NOT_added_when_disabled(self):
        eff = pipelock_effective_allowlist(_bottle({}))
        self.assertNotIn("supervise", eff)
        eff_explicit = pipelock_effective_allowlist(_bottle({"supervise": False}))
        self.assertNotIn("supervise", eff_explicit)


class TestTlsPassthrough(unittest.TestCase):
    def test_default_includes_api_anthropic(self):
        passthrough = pipelock_effective_tls_passthrough(_bottle({}))
        self.assertEqual(["api.anthropic.com"], passthrough)

    def test_route_hosts_NOT_added_to_passthrough(self):
        # cred-proxy now trusts pipelock's per-bottle CA, so pipelock
        # can MITM the cred-proxy -> upstream leg and body-scan it.
        # Auto-adding cred-proxy hosts to passthrough would silently
        # disable that second scanner.
        passthrough = pipelock_effective_tls_passthrough(_bottle(_routes([
            {"path": "/gh-api/", "upstream": "https://api.github.com",
             "auth_scheme": "Bearer", "token_ref": "G"},
            {"path": "/npm/", "upstream": "https://registry.npmjs.org",
             "auth_scheme": "Bearer", "token_ref": "N"},
        ])))
        self.assertEqual(["api.anthropic.com"], passthrough)


if __name__ == "__main__":
    unittest.main()
