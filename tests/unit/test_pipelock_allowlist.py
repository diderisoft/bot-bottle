"""Unit: pipelock_effective_allowlist — the union of baked-in defaults,
bottle.egress.allowlist, and cred-proxy upstream hosts derived from
bottle.tokens (PRD 0010). Git upstreams declared in bottle.git do not
contribute here; they flow through the per-agent git-gate (PRD 0008)."""

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


class TestTokenHosts(unittest.TestCase):
    def test_github_yields_both_hosts(self):
        hosts = pipelock_token_hosts(_bottle({
            "tokens": [{"Kind": "github", "TokenRef": "GH"}],
        }))
        self.assertEqual(["api.github.com", "github.com"], hosts)

    def test_gitea_yields_configured_host(self):
        hosts = pipelock_token_hosts(_bottle({
            "tokens": [{"Kind": "gitea", "TokenRef": "T",
                        "Url": "https://gitea.dideric.is"}],
        }))
        self.assertEqual(["gitea.dideric.is"], hosts)

    def test_npm_yields_registry(self):
        hosts = pipelock_token_hosts(_bottle({
            "tokens": [{"Kind": "npm", "TokenRef": "N"}],
        }))
        self.assertEqual(["registry.npmjs.org"], hosts)

    def test_anthropic_yields_api_host(self):
        hosts = pipelock_token_hosts(_bottle({
            "tokens": [{"Kind": "anthropic", "TokenRef": "A"}],
        }))
        self.assertEqual(["api.anthropic.com"], hosts)

    def test_no_tokens_empty(self):
        self.assertEqual([], pipelock_token_hosts(_bottle({})))


class TestAllowlistWithTokens(unittest.TestCase):
    def test_token_hosts_added_to_allowlist(self):
        eff = pipelock_effective_allowlist(_bottle({
            "tokens": [
                {"Kind": "npm", "TokenRef": "N"},
                {"Kind": "github", "TokenRef": "G"},
            ],
        }))
        self.assertIn("registry.npmjs.org", eff)
        self.assertIn("api.github.com", eff)
        self.assertIn("github.com", eff)

    def test_gitea_host_added(self):
        eff = pipelock_effective_allowlist(_bottle({
            "tokens": [{"Kind": "gitea", "TokenRef": "T",
                        "Url": "https://gitea.dideric.is"}],
        }))
        self.assertIn("gitea.dideric.is", eff)


class TestTlsPassthrough(unittest.TestCase):
    def test_default_includes_api_anthropic(self):
        passthrough = pipelock_effective_tls_passthrough(_bottle({}))
        self.assertEqual(["api.anthropic.com"], passthrough)

    def test_token_hosts_NOT_added_to_passthrough(self):
        # cred-proxy now trusts pipelock's per-bottle CA (loaded into
        # its container's trust store via docker cp + update-ca-
        # certificates at start time), so pipelock can MITM the
        # cred-proxy -> upstream leg and body-scan it. Auto-adding
        # cred-proxy hosts to passthrough would silently disable that
        # second scanner for github / gitea / npm.
        passthrough = pipelock_effective_tls_passthrough(_bottle({
            "tokens": [
                {"Kind": "github", "TokenRef": "G"},
                {"Kind": "npm", "TokenRef": "N"},
                {"Kind": "gitea", "TokenRef": "T",
                 "Url": "https://gitea.dideric.is"},
            ],
        }))
        self.assertEqual(["api.anthropic.com"], passthrough)


if __name__ == "__main__":
    unittest.main()
