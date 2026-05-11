"""Unit: allowlist resolution — pipelock_bottle_allowlist,
pipelock_bottle_ssh_hostnames, pipelock_bottle_ssh_ip_cidrs,
pipelock_bottle_ssh_trusted_domains, pipelock_effective_allowlist."""

import unittest

from claude_bottle.log import Die
from claude_bottle.manifest import Manifest
from claude_bottle.pipelock import (
    pipelock_bottle_allowlist,
    pipelock_bottle_ssh_hostnames,
    pipelock_bottle_ssh_ip_cidrs,
    pipelock_bottle_ssh_trusted_domains,
    pipelock_effective_allowlist,
)
from tests.fixtures import fixture_minimal, fixture_with_egress, fixture_with_ssh


class TestBottleAllowlist(unittest.TestCase):
    def test_egress_allowlist_present(self):
        out = pipelock_bottle_allowlist(fixture_with_egress().bottles["dev"])
        self.assertIn("github.com", out)
        self.assertIn("gitlab.com", out)
        self.assertIn("registry.npmjs.org", out)

    def test_empty_when_no_egress_block(self):
        out = pipelock_bottle_allowlist(fixture_minimal().bottles["dev"])
        self.assertEqual([], out)

    def test_rejects_non_string_entry(self):
        bad = {
            "bottles": {"dev": {"egress": {"allowlist": ["github.com", 42]}}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        }
        with self.assertRaises(Die):
            Manifest.from_json_obj(bad)


class TestSSHHostnames(unittest.TestCase):
    def test_hostnames_include_both(self):
        hosts = pipelock_bottle_ssh_hostnames(fixture_with_ssh().bottles["dev"])
        self.assertIn("192.0.2.10", hosts)
        self.assertIn("github.com", hosts)

    def test_ip_cidrs_only_ipv4(self):
        cidrs = pipelock_bottle_ssh_ip_cidrs(fixture_with_ssh().bottles["dev"])
        self.assertIn("192.0.2.10/32", cidrs)
        self.assertNotIn("github.com", cidrs)

    def test_trusted_domains_only_hostnames(self):
        trusted = pipelock_bottle_ssh_trusted_domains(fixture_with_ssh().bottles["dev"])
        self.assertIn("github.com", trusted)
        self.assertNotIn("192.0.2.10", trusted)


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
        self.assertIn("api.anthropic.com", eff)
        self.assertIn("registry.npmjs.org", eff)
        self.assertIn("192.0.2.10", eff)
        self.assertIn("github.com", eff)
        self.assertEqual(len(eff), len(set(eff)), "deduplicated")
        self.assertEqual(eff, sorted(eff), "sorted")


if __name__ == "__main__":
    unittest.main()
