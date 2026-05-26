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
from claude_bottle.pipelock import (
    DEFAULT_TLS_PASSTHROUGH,
    pipelock_build_config,
    pipelock_render_yaml,
)
from tests.fixtures import fixture_minimal


class TestBuildConfig(unittest.TestCase):
    def test_minimal_shape(self):
        cfg = pipelock_build_config(fixture_minimal().bottles["dev"])
        self.assertEqual("strict", cfg["mode"])
        self.assertEqual(True, cfg["enforce"])
        self.assertEqual({"enabled": True}, cfg["forward_proxy"])
        self.assertEqual(
            {"include_defaults": True, "scan_env": True}, cfg["dlp"]
        )
        # Body-scan action is hard-coded "block" in pipelock_build_config.
        self.assertEqual(
            {"action": "block"}, cfg["request_body_scanning"]
        )
        # Baked defaults always present.
        self.assertIn("api.anthropic.com", cast(list[str], cfg["api_allowlist"]))
        self.assertIn("raw.githubusercontent.com", cast(list[str], cfg["api_allowlist"]))
        # pipelock has no SSH carve-outs at all — neither
        # trusted_domains nor ssrf are emitted from bottle data.
        self.assertNotIn("trusted_domains", cfg)
        self.assertNotIn("ssrf", cfg)
        # Without CA paths, the tls_interception block is omitted —
        # pipelock falls back to its built-in default of `enabled: false`.
        self.assertNotIn("tls_interception", cfg)

    def test_tls_interception_block_emitted_when_paths_supplied(self):
        # PRD 0006: paths flow in via DockerPipelockProxy's in-container
        # constants; this directly pins the dict shape. passthrough_domains
        # is baked in so LLM provider endpoints (api.anthropic.com) skip
        # MITM — pipelock's docs explicitly recommend this for LLM hosts,
        # and without it the BIP-39 body scanner false-positives on
        # Claude conversation traffic.
        cfg = pipelock_build_config(
            fixture_minimal().bottles["dev"],
            ca_cert_path="/etc/pipelock-ca.pem",
            ca_key_path="/etc/pipelock-ca-key.pem",
        )
        self.assertEqual(
            {
                "enabled": True,
                "ca_cert": "/etc/pipelock-ca.pem",
                "ca_key": "/etc/pipelock-ca-key.pem",
                "passthrough_domains": list(DEFAULT_TLS_PASSTHROUGH),
            },
            cfg["tls_interception"],
        )
        self.assertIn("api.anthropic.com", DEFAULT_TLS_PASSTHROUGH)

    def test_tls_interception_requires_both_paths(self):
        # Half-set is a programmer error, not a silent omission.
        with self.assertRaises(ValueError):
            pipelock_build_config(
                fixture_minimal().bottles["dev"],
                ca_cert_path="/etc/pipelock-ca.pem",
            )

    def test_ssrf_block_omitted_when_no_allowlist(self):
        cfg = pipelock_build_config(fixture_minimal().bottles["dev"])
        self.assertNotIn("ssrf", cfg)

    def test_ssrf_block_emitted_when_allowlist_supplied(self):
        # The bottle's internal Docker subnet lands here at launch
        # time so sibling-sidecar traffic (172.x.x.x) doesn't trip
        # pipelock's RFC1918 SSRF guard.
        cfg = pipelock_build_config(
            fixture_minimal().bottles["dev"],
            ssrf_ip_allowlist=("172.20.0.0/16",),
        )
        self.assertIn("ssrf", cfg)
        self.assertEqual({"ip_allowlist": ["172.20.0.0/16"]}, cfg["ssrf"])

    def test_seed_phrase_detection_left_at_default_when_no_anthropic_route(self):
        # No override emitted -> pipelock keeps its built-in default
        # (BIP-39 detection enabled). Bottles that don't carry an
        # Anthropic route don't need the false-positive workaround.
        cfg = pipelock_build_config(fixture_minimal().bottles["dev"])
        self.assertNotIn("seed_phrase_detection", cfg)

    def test_seed_phrase_detection_disabled_for_anthropic_route(self):
        # claude-code's chat bodies trip pipelock's BIP-39 detector
        # (12+ English words that pass the checksum). pipelock 2.3.0
        # has no per-path knob for this detector, and both `suppress`
        # and `rules.disabled` only silence alerts — the block still
        # fires. The only knob that actually skips the block is the
        # global on/off, so we flip it off whenever the bottle is set
        # up to route claude through pipelock.
        from claude_bottle.manifest import Manifest
        bottle = Manifest.from_json_obj({
            "bottles": {"dev": {"egress": {"routes": [
                {"host": "api.anthropic.com",
                 "auth": {"scheme": "Bearer", "token_ref": "T"}},
            ]}}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        }).bottles["dev"]
        cfg = pipelock_build_config(bottle)
        self.assertEqual({"enabled": False}, cfg["seed_phrase_detection"])


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
        cfg = pipelock_build_config(fixture_minimal().bottles["dev"])
        text = pipelock_render_yaml(cfg)
        for required in (
            "api_allowlist:",
            "forward_proxy:",
            "dlp:",
            "request_body_scanning:",
        ):
            self.assertIn(required, text)
        # No ssh carve-outs in the rendered yaml.
        self.assertNotIn("trusted_domains:", text)
        self.assertNotIn("ssrf:", text)

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
                    "egress": {"routes": [{"host": "github.com"}]},
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

    def test_render_emits_tls_interception_via_prepare(self):
        """`DockerPipelockProxy.prepare` plumbs its in-container CA
        constants through to the YAML. The block should land in the
        rendered output with `enabled: true`, the configured paths,
        and the baked LLM-provider passthrough list. The actual
        host-side CA generation happens in launch (not prepare), so
        this test exercises only the YAML rendering."""
        plan = DockerPipelockProxy().prepare(
            fixture_minimal().bottles["dev"], "demo", self.out_dir
        )
        content = plan.yaml_path.read_text()
        self.assertIn("tls_interception:", content)
        self.assertIn("enabled: true", content)
        self.assertIn('ca_cert: "/etc/pipelock-ca.pem"', content)
        self.assertIn('ca_key: "/etc/pipelock-ca-key.pem"', content)
        self.assertIn("passthrough_domains:", content)
        self.assertIn('- "api.anthropic.com"', content)

    def test_render_emits_ssrf_block_when_allowlist_given(self):
        cfg = pipelock_build_config(
            fixture_minimal().bottles["dev"],
            ca_cert_path="/etc/pipelock-ca.pem",
            ca_key_path="/etc/pipelock-ca-key.pem",
            ssrf_ip_allowlist=("172.20.0.0/16",),
        )
        text = pipelock_render_yaml(cfg)
        self.assertIn("ssrf:", text)
        self.assertIn("ip_allowlist:", text)
        self.assertIn('- "172.20.0.0/16"', text)

    def test_render_emits_seed_phrase_off_for_anthropic_route(self):
        from claude_bottle.manifest import Manifest
        bottle = Manifest.from_json_obj({
            "bottles": {"dev": {"egress": {"routes": [
                {"host": "api.anthropic.com",
                 "auth": {"scheme": "Bearer", "token_ref": "T"}},
            ]}}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        }).bottles["dev"]
        text = pipelock_render_yaml(pipelock_build_config(bottle))
        self.assertIn("seed_phrase_detection:", text)
        self.assertIn("enabled: false", text)


if __name__ == "__main__":
    unittest.main()
