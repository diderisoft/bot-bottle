"""Unit: pipelock_write_yaml — produces a YAML config containing the
expected top-level keys and per-bottle entries. We don't fully parse
YAML; we grep for content shape."""

import os
import tempfile
import unittest
from pathlib import Path

from claude_bottle.manifest import Manifest
from claude_bottle.pipelock import pipelock_write_yaml
from tests.fixtures import fixture_minimal, fixture_with_ssh


class TestPipelockYaml(unittest.TestCase):
    def setUp(self):
        self.out_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.out_dir, ignore_errors=True)

    def test_minimal(self):
        yaml_path = self.out_dir / "min.yaml"
        pipelock_write_yaml(fixture_minimal(), "dev", yaml_path)
        content = yaml_path.read_text()
        self.assertIn("mode: strict", content)
        self.assertIn("enforce: true", content)
        self.assertIn("api_allowlist:", content)
        self.assertIn("api.anthropic.com", content)
        self.assertIn("raw.githubusercontent.com", content)
        self.assertIn("forward_proxy:", content)
        self.assertIn("enabled: true", content)
        self.assertIn("dlp:", content)
        self.assertIn("include_defaults: true", content)
        self.assertIn("scan_env: true", content)
        # No ssh entries → no trusted_domains nor ssrf block.
        self.assertNotIn("trusted_domains:", content)
        self.assertNotIn("ssrf:", content)

    def test_ssh_blocks(self):
        yaml_path = self.out_dir / "ssh.yaml"
        pipelock_write_yaml(fixture_with_ssh(), "dev", yaml_path)
        content = yaml_path.read_text()
        self.assertIn("trusted_domains:", content)
        self.assertIn("github.com", content)
        self.assertIn("ssrf:", content)
        self.assertIn("ip_allowlist:", content)
        self.assertIn("192.0.2.10/32", content)
        # ipv4 host should also be in api_allowlist (strict mode requires both).
        self.assertIn("192.0.2.10", content)

    def test_secret_hygiene(self):
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
        yaml_path = self.out_dir / "secret.yaml"
        pipelock_write_yaml(manifest, "dev", yaml_path)
        content = yaml_path.read_text()
        self.assertNotIn("literal-value-should-not-appear", content)
        self.assertNotIn("MY_SECRET", content)
        self.assertNotIn("prompt-message", content)

    def test_file_mode_is_600(self):
        yaml_path = self.out_dir / "min.yaml"
        pipelock_write_yaml(fixture_minimal(), "dev", yaml_path)
        mode = os.stat(yaml_path).st_mode & 0o777
        self.assertEqual(0o600, mode)


if __name__ == "__main__":
    unittest.main()
