"""Unit: pipelock_effective_allowlist — the union of baked-in defaults
and bottle.egress.allowlist. Per PRD 0007, bottle.ssh entries do NOT
contribute (SSH traffic goes through the per-agent ssh-gate, not
pipelock)."""

import unittest

from claude_bottle.manifest import Manifest
from claude_bottle.pipelock import pipelock_effective_allowlist


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
        # PRD 0007: ssh hostnames must not contribute to pipelock's
        # allowlist anymore — they're routed through the ssh-gate
        # sidecar, which is on its own egress path.
        self.assertNotIn("192.0.2.10", eff)
        self.assertNotIn("github.com", eff)
        self.assertEqual(len(eff), len(set(eff)), "deduplicated")
        self.assertEqual(eff, sorted(eff), "sorted")


if __name__ == "__main__":
    unittest.main()
