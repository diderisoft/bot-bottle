"""Unit: Smolfile renderer for the smolmachines backend (PRD 0023).

Pure-function tests on `smolfile_build` + `smolfile_render`. The
schema we emit is narrow (name + command + env + one inline-table
per net device), so the tests exhaustively cover what lands on
disk."""

from __future__ import annotations

import unittest
from pathlib import Path

from claude_bottle.backend.smolmachines.smolfile import (
    GVPROXY_PIPELOCK_GATEWAY_PORT,
    smolfile_build,
    smolfile_render,
)


class TestSmolfileBuild(unittest.TestCase):
    def _build(self, **kwargs):
        defaults = dict(
            slug="demo-abc12",
            gvproxy_socket=Path("/tmp/cb-stage/gvproxy.sock"),
            env={"HTTPS_PROXY": "http://proxy.internal:8888"},
        )
        defaults.update(kwargs)
        return smolfile_build(**defaults)

    def test_name_uses_claude_bottle_prefix(self):
        cfg = self._build(slug="myagent-xyz")
        self.assertEqual("claude-bottle-myagent-xyz", cfg["name"])

    def test_command_defaults_to_sleep_infinity(self):
        # Chunk 1 placeholder; chunk 4 swaps in the real claude
        # entrypoint.
        cfg = self._build()
        self.assertEqual(["sleep", "infinity"], cfg["command"])

    def test_command_can_be_overridden(self):
        cfg = self._build(command=("claude", "--no-banner"))
        self.assertEqual(["claude", "--no-banner"], cfg["command"])

    def test_env_renders_as_sorted_KEY_VALUE_list(self):
        cfg = self._build(env={
            "ZED": "one",
            "ALPHA": "two",
            "HTTPS_PROXY": "http://proxy.internal:8888",
        })
        # Sorted by key so renderer output is deterministic.
        self.assertEqual(
            ["ALPHA=two", "HTTPS_PROXY=http://proxy.internal:8888", "ZED=one"],
            cfg["env"],
        )

    def test_net_device_points_at_gvproxy_socket(self):
        cfg = self._build(gvproxy_socket=Path("/state/foo/gv.sock"))
        self.assertEqual(1, len(cfg["net"]))
        net = cfg["net"][0]
        self.assertEqual("virtio-net", net["type"])
        self.assertEqual("unixgram", net["attachment"])
        self.assertEqual("/state/foo/gv.sock", net["socket"])

    def test_no_tsi_flags(self):
        # PRD 0023: TSI is explicitly rejected. The Smolfile must
        # never carry --allow-cidr / --allow-host /
        # --outbound-localhost-only — gvproxy is the policy layer.
        cfg = self._build()
        rendered = smolfile_render(cfg)
        self.assertNotIn("--allow-cidr", rendered)
        self.assertNotIn("--allow-host", rendered)
        self.assertNotIn("--outbound-localhost-only", rendered)
        self.assertNotIn("tsi", rendered.lower())


class TestSmolfileRender(unittest.TestCase):
    """The rendered TOML must be parseable by stdlib `tomllib` and
    contain the keys the smolmachines schema expects."""

    def _render(self, **kwargs):
        cfg = smolfile_build(
            slug="demo-abc12",
            gvproxy_socket=Path("/tmp/gvp.sock"),
            env={"HTTPS_PROXY": "http://proxy.internal:8888"},
            **kwargs,
        )
        return smolfile_render(cfg)

    def test_round_trip_through_tomllib(self):
        import tomllib  # stdlib in 3.11+
        rendered = self._render()
        parsed = tomllib.loads(rendered)
        self.assertEqual("claude-bottle-demo-abc12", parsed["name"])
        self.assertEqual(["sleep", "infinity"], parsed["command"])
        self.assertIn("HTTPS_PROXY=http://proxy.internal:8888", parsed["env"])
        # net is an array of tables → list of dicts post-parse.
        self.assertEqual(1, len(parsed["net"]))
        self.assertEqual("/tmp/gvp.sock", parsed["net"][0]["socket"])

    def test_special_chars_in_values_escape_correctly(self):
        import tomllib
        cfg = smolfile_build(
            slug="demo",
            gvproxy_socket=Path("/tmp/path with spaces/gv.sock"),
            env={"WITH_QUOTES": 'has "double" quotes'},
        )
        rendered = smolfile_render(cfg)
        parsed = tomllib.loads(rendered)
        self.assertEqual(
            "/tmp/path with spaces/gv.sock",
            parsed["net"][0]["socket"],
        )
        # The env entry survives the quote escape.
        self.assertIn('WITH_QUOTES=has "double" quotes', parsed["env"])

    def test_constants_match_what_prepare_uses(self):
        # Lock the gateway-port constant so the prepare side and the
        # config-render side don't drift out of sync.
        self.assertEqual(8888, GVPROXY_PIPELOCK_GATEWAY_PORT)


if __name__ == "__main__":
    unittest.main()
