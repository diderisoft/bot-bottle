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
from typing import cast

from bot_bottle.manifest import Manifest
from bot_bottle.pipelock import (
    DEFAULT_TLS_PASSTHROUGH,
    PipelockProxy,
    pipelock_build_config,
    pipelock_render_yaml,
)
from bot_bottle.yaml_subset import parse_yaml_subset
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
        # `scan_headers: True` + `header_mode: "all"` close the
        # header-shape exfil gap surfaced by PRD 0022 attack 3.
        self.assertEqual(
            {
                "action": "block",
                "scan_headers": True,
                "header_mode": "all",
            },
            cfg["request_body_scanning"],
        )
        # No provider defaults are injected implicitly.
        self.assertEqual([], cast(list[str], cfg["api_allowlist"]))
        # pipelock has no SSH carve-outs at all — neither
        # trusted_domains nor ssrf are emitted from bottle data.
        self.assertNotIn("trusted_domains", cfg)
        self.assertNotIn("ssrf", cfg)
        # Without CA paths, the tls_interception block is omitted —
        # pipelock falls back to its built-in default of `enabled: false`.
        self.assertNotIn("tls_interception", cfg)

    def test_tls_interception_block_emitted_when_paths_supplied(self):
        # PRD 0006: paths flow in via the platform-neutral in-container
        # constants; this directly pins the dict shape.
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
                "passthrough_domains": [],
            },
            cfg["tls_interception"],
        )
        self.assertEqual((), DEFAULT_TLS_PASSTHROUGH)

    def test_tls_passthrough_route_policy_emits_domain(self):
        bottle = Manifest.from_json_obj({
            "bottles": {"dev": {"egress": {"routes": [
                {"host": "api.openai.com",
                 "auth": {"scheme": "Bearer", "token_ref": "T"},
                 "pipelock": {"tls_passthrough": True}},
            ]}}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        }).bottles["dev"]
        cfg = pipelock_build_config(
            bottle,
            ca_cert_path="/etc/pipelock-ca.pem",
            ca_key_path="/etc/pipelock-ca-key.pem",
        )
        tls = cast(dict[str, object], cfg["tls_interception"])
        self.assertEqual(["api.openai.com"], tls["passthrough_domains"])

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

    def test_ssrf_block_emitted_from_route_policy(self):
        bottle = Manifest.from_json_obj({
            "bottles": {"dev": {"egress": {"routes": [
                {"host": "gitea.dideric.is",
                 "pipelock": {"ssrf_ip_allowlist": ["192.0.2.10/32"]}},
            ]}}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        }).bottles["dev"]
        cfg = pipelock_build_config(bottle)
        self.assertEqual(
            {"ip_allowlist": ["192.0.2.10/32"]},
            cfg["ssrf"],
        )

    def test_seed_phrase_detection_disabled_by_default(self):
        # Only the broad BIP-39 detector is disabled. The rest of
        # DLP remains enabled via the `dlp` and request-body sections.
        cfg = pipelock_build_config(fixture_minimal().bottles["dev"])
        self.assertEqual({"enabled": False}, cfg["seed_phrase_detection"])

    def test_seed_phrase_detection_disabled_for_openai_route(self):
        # OpenAI/Codex chat bodies trip pipelock's BIP-39 detector
        # (12+ English words that pass the checksum). pipelock 2.3.0
        # has no per-path knob for this detector, and both `suppress`
        # and `rules.disabled` only silence alerts — the block still
        # fires. The only knob that actually skips the block is the
        # global on/off.
        from bot_bottle.manifest import Manifest
        bottle = Manifest.from_json_obj({
            "bottles": {"dev": {"egress": {"routes": [
                {"host": "api.openai.com",
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

    def assert_render_semantics_match(self, cfg: dict[str, object]) -> None:
        parsed = parse_yaml_subset(pipelock_render_yaml(cfg))
        self.assertEqual(cfg["version"], parsed["version"])
        self.assertEqual(cfg["mode"], parsed["mode"])
        self.assertEqual(cfg["enforce"], parsed["enforce"])
        parsed_allowlist = parsed["api_allowlist"]
        if cfg["api_allowlist"] == [] and parsed_allowlist is None:
            parsed_allowlist = []
        self.assertEqual(cfg["api_allowlist"], parsed_allowlist)
        self.assertEqual(cfg["forward_proxy"], parsed["forward_proxy"])
        self.assertEqual(cfg["dlp"], parsed["dlp"])
        self.assertEqual(
            cfg["request_body_scanning"],
            parsed["request_body_scanning"],
        )
        if "seed_phrase_detection" in cfg:
            self.assertEqual(
                cfg["seed_phrase_detection"],
                parsed["seed_phrase_detection"],
            )
        else:
            self.assertNotIn("seed_phrase_detection", parsed)

        if "tls_interception" in cfg:
            expected_tls = cast(dict[str, object], cfg["tls_interception"])
            actual_tls = cast(dict[str, object], parsed["tls_interception"])
            self.assertEqual(expected_tls["enabled"], actual_tls["enabled"])
            self.assertEqual(expected_tls["ca_cert"], actual_tls["ca_cert"])
            self.assertEqual(expected_tls["ca_key"], actual_tls["ca_key"])
            expected_passthrough = expected_tls["passthrough_domains"]
            if expected_passthrough:
                self.assertEqual(
                    expected_passthrough,
                    actual_tls["passthrough_domains"],
                )
            else:
                self.assertNotIn("passthrough_domains", actual_tls)
        else:
            self.assertNotIn("tls_interception", parsed)

        if "ssrf" in cfg:
            self.assertEqual(cfg["ssrf"], parsed["ssrf"])
        else:
            self.assertNotIn("ssrf", parsed)

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

    def test_render_semantics_match_minimal_config(self):
        cfg = pipelock_build_config(fixture_minimal().bottles["dev"])
        self.assert_render_semantics_match(cfg)

    def test_render_semantics_match_tls_with_empty_passthrough(self):
        cfg = pipelock_build_config(
            fixture_minimal().bottles["dev"],
            ca_cert_path="/etc/pipelock-ca.pem",
            ca_key_path="/etc/pipelock-ca-key.pem",
        )
        self.assert_render_semantics_match(cfg)

    def test_render_semantics_match_all_optional_sections(self):
        bottle = Manifest.from_json_obj({
            "bottles": {"dev": {"egress": {"routes": [
                {"host": "api.openai.com",
                 "pipelock": {"tls_passthrough": True}},
                {"host": "gitea.dideric.is",
                 "pipelock": {"ssrf_ip_allowlist": ["192.0.2.10/32"]}},
            ]}}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        }).bottles["dev"]
        cfg = pipelock_build_config(
            bottle,
            ca_cert_path="/etc/pipelock-ca.pem",
            ca_key_path="/etc/pipelock-ca-key.pem",
            ssrf_ip_allowlist=("172.20.0.0/16",),
        )
        self.assert_render_semantics_match(cfg)

    def test_render_rejects_missing_required_key(self):
        cfg = pipelock_build_config(fixture_minimal().bottles["dev"])
        del cfg["mode"]
        with self.assertRaisesRegex(ValueError, r"config\.mode"):
            pipelock_render_yaml(cfg)

    def test_render_rejects_wrong_section_type(self):
        cfg = pipelock_build_config(fixture_minimal().bottles["dev"])
        cfg["dlp"] = []
        with self.assertRaisesRegex(ValueError, r"config\.dlp.*mapping"):
            pipelock_render_yaml(cfg)

    def test_render_rejects_wrong_list_item_type(self):
        cfg = pipelock_build_config(
            fixture_minimal().bottles["dev"],
            ca_cert_path="/etc/pipelock-ca.pem",
            ca_key_path="/etc/pipelock-ca-key.pem",
        )
        tls = cast(dict[str, object], cfg["tls_interception"])
        tls["passthrough_domains"] = ["api.openai.com", 3]
        with self.assertRaisesRegex(
            ValueError, r"tls_interception\.passthrough_domains",
        ):
            pipelock_render_yaml(cfg)

    def test_render_rejects_unsupported_top_level_section(self):
        cfg = pipelock_build_config(fixture_minimal().bottles["dev"])
        cfg["trusted_domains"] = []
        with self.assertRaisesRegex(ValueError, r"config\.trusted_domains"):
            pipelock_render_yaml(cfg)

    def test_prepare_writes_file_at_mode_600(self):
        plan = PipelockProxy().prepare(
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
        plan = PipelockProxy().prepare(
            manifest.bottles["dev"], "demo", self.out_dir
        )
        content = plan.yaml_path.read_text()
        self.assertNotIn("literal-value-should-not-appear", content)
        self.assertNotIn("MY_SECRET", content)
        self.assertNotIn("prompt-message", content)

    def test_render_emits_tls_interception_via_prepare(self):
        """`PipelockProxy.prepare` plumbs the module-level in-container
        CA constants through to the YAML. The block should land in the
        rendered output with `enabled: true`, the configured paths,
        and any route-owned passthrough domains. The actual
        host-side CA generation happens in launch (not prepare), so
        this test exercises only the YAML rendering."""
        bottle = Manifest.from_json_obj({
            "bottles": {"dev": {"egress": {"routes": [
                {"host": "api.openai.com",
                 "pipelock": {"tls_passthrough": True}},
            ]}}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        }).bottles["dev"]
        plan = PipelockProxy().prepare(bottle, "demo", self.out_dir)
        content = plan.yaml_path.read_text()
        self.assertIn("tls_interception:", content)
        self.assertIn("enabled: true", content)
        self.assertIn('ca_cert: "/etc/pipelock-ca.pem"', content)
        self.assertIn('ca_key: "/etc/pipelock-ca-key.pem"', content)
        self.assertIn("passthrough_domains:", content)
        self.assertIn('- "api.openai.com"', content)

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

    def test_render_emits_seed_phrase_off_by_default(self):
        text = pipelock_render_yaml(
            pipelock_build_config(fixture_minimal().bottles["dev"])
        )
        self.assertIn("seed_phrase_detection:", text)
        self.assertIn("enabled: false", text)


if __name__ == "__main__":
    unittest.main()
