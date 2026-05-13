"""Unit: pipelock_effective_allowlist — the union of baked-in defaults
and bottle.egress.allowlist. Git upstreams declared in bottle.git do not
contribute here; they flow through the per-agent git-gate (PRD 0008)."""

import unittest

from claude_bottle.manifest import Manifest
from claude_bottle.pipelock import pipelock_effective_allowlist


class TestEffectiveAllowlist(unittest.TestCase):
    def test_union_and_dedup(self):
        manifest = Manifest.from_json_obj({
            "bottles": {
                "dev": {
                    "egress": {
                        "allowlist": [
                            "registry.npmjs.org",
                            # Duplicate of a baked default; the union
                            # must dedupe.
                            "api.anthropic.com",
                        ],
                    },
                },
            },
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        eff = pipelock_effective_allowlist(manifest.bottles["dev"])
        self.assertIn("api.anthropic.com", eff, "baked default present")
        self.assertIn("registry.npmjs.org", eff, "egress.allowlist present")
        self.assertEqual(len(eff), len(set(eff)), "deduplicated")
        self.assertEqual(eff, sorted(eff), "sorted")


if __name__ == "__main__":
    unittest.main()
