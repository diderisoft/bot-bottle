"""Unit: pipelock config building and YAML rendering.

`pipelock_build_config` produces the structured config dict pipelock
will load; tests assert on that dict so they don't break on cosmetic
YAML changes. A small set of tests still hit the rendered output for
properties that only make sense on disk (file mode, no-secret-leakage).
"""

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from claude_bottle.backend.docker.pipelock import DockerPipelockProxy
from claude_bottle.manifest import Manifest
from claude_bottle.pipelock import pipelock_build_config, pipelock_render_yaml
from tests.fixtures import fixture_minimal, fixture_with_ssh


class TestBuildConfig(unittest.TestCase):
    def test_minimal_shape(self):
        cfg = pipelock_build_config(fixture_minimal().bottles["dev"])
        self.assertEqual("strict", cfg["mode"])
        self.assertEqual(True, cfg["enforce"])
        self.assertEqual({"enabled": True}, cfg["forward_proxy"])
        self.assertEqual(
            {"include_defaults": True, "scan_env": True}, cfg["dlp"]
        )
        # Default body-scan action is "block" — see BottleEgress.dlp_action.
        self.assertEqual(
            {"action": "block"}, cfg["request_body_scanning"]
        )
        # Baked defaults always present.
        self.assertIn("api.anthropic.com", cast(list[str], cfg["api_allowlist"]))
        self.assertIn("raw.githubusercontent.com", cast(list[str], cfg["api_allowlist"]))
        # No SSH entries → no trusted_domains, no ssrf.
        self.assertNotIn("trusted_domains", cfg)
        self.assertNotIn("ssrf", cfg)

    def test_ssh_shape(self):
        cfg = pipelock_build_config(fixture_with_ssh().bottles["dev"])
        self.assertIn("github.com", cast(list[str], cfg["trusted_domains"]))
        self.assertNotIn("192.0.2.10", cast(list[str], cfg["trusted_domains"]))
        self.assertIn(
            "192.0.2.10/32",
            cast(dict[str, Any], cfg["ssrf"])["ip_allowlist"],
        )
        # Strict mode: IPv4 host is also in the api_allowlist union.
        self.assertIn("192.0.2.10", cast(list[str], cfg["api_allowlist"]))


class TestRenderAndWrite(unittest.TestCase):
    def setUp(self):
        self.out_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.out_dir, ignore_errors=True)

    def test_render_emits_required_top_level_keys(self):
        """One render-level smoke check: the serialized YAML is plausibly
        the shape pipelock expects. We don't grep every key here — that's
        what TestBuildConfig is for."""
        cfg = pipelock_build_config(fixture_with_ssh().bottles["dev"])
        text = pipelock_render_yaml(cfg)
        for required in (
            "api_allowlist:",
            "forward_proxy:",
            "trusted_domains:",
            "ssrf:",
            "dlp:",
            "request_body_scanning:",
        ):
            self.assertIn(required, text)

    def test_prepare_writes_file_at_mode_600(self):
        plan = DockerPipelockProxy().prepare(
            fixture_minimal().bottles["dev"], "demo", self.out_dir
        )
        self.assertEqual(0o600, os.stat(plan.yaml_path).st_mode & 0o777)

    def test_prepare_does_not_leak_env_names_or_values(self):
        manifest = Manifest.from_json_obj({
            "bottles": {
                "dev": {
                    "env": {
                        "MY_SECRET": "literal-value-should-not-appear",
                        "ANOTHER": "?prompt-message",
                    },
                    "egress": {"allowlist": ["github.com"]},
                }
            },
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        plan = DockerPipelockProxy().prepare(
            manifest.bottles["dev"], "demo", self.out_dir
        )
        content = plan.yaml_path.read_text()
        self.assertNotIn("literal-value-should-not-appear", content)
        self.assertNotIn("MY_SECRET", content)
        self.assertNotIn("prompt-message", content)


if __name__ == "__main__":
    unittest.main()
