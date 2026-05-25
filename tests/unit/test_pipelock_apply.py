"""Unit: pipelock_apply parsers + helpers (PRD 0015 Phase 1).

docker exec / cp / restart paths are covered by the integration
test in Phase 4. Here we cover the host-side parsing + yaml roundtrip.
"""

import unittest

from claude_bottle.backend.docker.pipelock_apply import (
    PipelockApplyError,
    parse_allowlist_content,
    render_allowlist_content,
)
from claude_bottle.pipelock import pipelock_render_yaml
from claude_bottle.yaml_subset import parse_yaml_subset


class TestParseAllowlistContent(unittest.TestCase):
    def test_one_per_line(self):
        self.assertEqual(
            ["a.example", "b.example"],
            parse_allowlist_content("a.example\nb.example\n"),
        )

    def test_blank_lines_ignored(self):
        self.assertEqual(
            ["a", "b"],
            parse_allowlist_content("a\n\n  \nb\n"),
        )

    def test_comments_ignored(self):
        self.assertEqual(
            ["a"],
            parse_allowlist_content("# top comment\na\n# trailing\n"),
        )

    def test_invalid_char_raises(self):
        with self.assertRaises(PipelockApplyError) as cm:
            parse_allowlist_content("host with space\n")
        self.assertIn("disallowed characters", str(cm.exception))

    def test_empty_input_returns_empty_list(self):
        self.assertEqual([], parse_allowlist_content(""))


class TestRenderAllowlistContent(unittest.TestCase):
    def test_one_per_line_with_trailing_newline(self):
        self.assertEqual("a\nb\n", render_allowlist_content(["a", "b"]))

    def test_empty_renders_empty(self):
        self.assertEqual("", render_allowlist_content([]))

    def test_roundtrip(self):
        original = ["api.example.com", "ghcr.io", "example.org"]
        self.assertEqual(
            original,
            parse_allowlist_content(render_allowlist_content(original)),
        )


class TestYamlRoundtripPreservesPipelockFields(unittest.TestCase):
    """The apply path parses the running pipelock.yaml, swaps
    api_allowlist, re-renders. Verify that parse(render(cfg)) ==
    cfg for the fields pipelock_render_yaml emits — otherwise
    the apply would silently drop config."""

    def test_minimal_config_roundtrips(self):
        cfg = {
            "version": 1,
            "mode": "strict",
            "enforce": True,
            "api_allowlist": ["a.example", "b.example"],
            "forward_proxy": {"enabled": True},
            "dlp": {"include_defaults": True, "scan_env": True},
            "request_body_scanning": {"action": "block"},
        }
        rendered = pipelock_render_yaml(cfg)
        parsed = parse_yaml_subset(rendered)
        self.assertEqual(["a.example", "b.example"], parsed["api_allowlist"])
        self.assertEqual(1, parsed["version"])
        self.assertEqual("strict", parsed["mode"])
        self.assertEqual(True, parsed["enforce"])

    def test_swap_allowlist_then_render_preserves_other_fields(self):
        cfg = {
            "version": 1,
            "mode": "strict",
            "enforce": True,
            "api_allowlist": ["old.example"],
            "forward_proxy": {"enabled": True},
            "dlp": {"include_defaults": True, "scan_env": True},
            "request_body_scanning": {"action": "block"},
            "tls_interception": {
                "enabled": True,
                "ca_cert": "/etc/pipelock-ca.pem",
                "ca_key": "/etc/pipelock-ca-key.pem",
                "passthrough_domains": ["api.anthropic.com"],
            },
        }
        parsed = parse_yaml_subset(pipelock_render_yaml(cfg))
        parsed["api_allowlist"] = ["new.example"]
        rerendered = pipelock_render_yaml(parsed)
        roundtripped = parse_yaml_subset(rerendered)
        self.assertEqual(["new.example"], roundtripped["api_allowlist"])
        # Non-allowlist fields stay put.
        self.assertEqual("strict", roundtripped["mode"])
        tls = roundtripped["tls_interception"]
        self.assertIsInstance(tls, dict)
        assert isinstance(tls, dict)  # type-narrowing
        self.assertEqual("/etc/pipelock-ca.pem", tls["ca_cert"])
        self.assertEqual(["api.anthropic.com"], tls["passthrough_domains"])


if __name__ == "__main__":
    unittest.main()
