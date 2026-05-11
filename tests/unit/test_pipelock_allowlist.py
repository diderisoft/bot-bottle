"""Unit: pipelock_effective_allowlist — the union of baked-in defaults,
bottle.egress.allowlist, and bottle.ssh[].Hostname. Plus a small check
that IPv4 hostnames pick up the /32 suffix when classified as CIDRs.

The lower-level one-line helpers (pipelock_bottle_allowlist,
pipelock_bottle_ssh_hostnames, pipelock_bottle_ssh_trusted_domains)
are exercised end-to-end by test_union_and_dedup, so they don't get
their own tests."""

import unittest

from claude_bottle.manifest import Manifest
from claude_bottle.pipelock import (
    pipelock_bottle_ssh_ip_cidrs,
    pipelock_effective_allowlist,
)
from tests.fixtures import fixture_with_ssh


class TestEffectiveAllowlist(unittest.TestCase):
    def test_union_and_dedup(self):
        manifest = Manifest.from_json_obj({
            "bottles": {
                "dev": {
                    "egress": {"allowlist": ["registry.npmjs.org"]},
                    "ssh": [
                        {"Host": "ts", "IdentityFile": "/dev/null",
                         "Hostname": "192.0.2.10", "User": "git", "Port": 30009},
                        {"Host": "gh", "IdentityFile": "/dev/null",
                         "Hostname": "github.com", "User": "git", "Port": 22},
                    ],
                }
            },
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        eff = pipelock_effective_allowlist(manifest.bottles["dev"])
        self.assertIn("api.anthropic.com", eff, "baked default present")
        self.assertIn("registry.npmjs.org", eff, "egress.allowlist present")
        self.assertIn("192.0.2.10", eff, "ssh ipv4 hostname present")
        self.assertIn("github.com", eff, "ssh hostname present")
        self.assertEqual(len(eff), len(set(eff)), "deduplicated")
        self.assertEqual(eff, sorted(eff), "sorted")


class TestSSHIPCidrs(unittest.TestCase):
    def test_ipv4_hostname_gets_32_suffix(self):
        cidrs = pipelock_bottle_ssh_ip_cidrs(fixture_with_ssh().bottles["dev"])
        self.assertIn("192.0.2.10/32", cidrs)
        # Hostname-typed entries don't end up here.
        self.assertNotIn("github.com", cidrs)
        self.assertNotIn("github.com/32", cidrs)


if __name__ == "__main__":
    unittest.main()
