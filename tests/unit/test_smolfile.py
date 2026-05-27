"""Unit: Smolfile renderer for the smolmachines backend (PRD 0023).

Pure-function tests on `smolfile_build` + `smolfile_render`. The
schema we emit is narrow (env list + `[network] allow_cidrs`), so
the tests exhaustively cover what lands on disk."""

from __future__ import annotations

import unittest

from claude_bottle.backend.smolmachines.smolfile import (
    smolfile_build,
    smolfile_render,
)


class TestSmolfileBuild(unittest.TestCase):
    def _build(self, **kwargs):
        defaults = dict(
            env={"HTTPS_PROXY": "http://192.168.50.2:8888"},
            bundle_ip="192.168.50.2",
        )
        defaults.update(kwargs)
        return smolfile_build(**defaults)

    def test_env_renders_as_sorted_KEY_VALUE_list(self):
        # Sorted by key so renderer output is deterministic.
        cfg = self._build(env={
            "ZED": "one",
            "ALPHA": "two",
            "HTTPS_PROXY": "http://192.168.50.2:8888",
        })
        self.assertEqual(
            [
                "ALPHA=two",
                "HTTPS_PROXY=http://192.168.50.2:8888",
                "ZED=one",
            ],
            cfg["env"],
        )

    def test_allow_cidrs_is_single_slash_32(self):
        # TSI's single-IP allowlist. Anything else would
        # re-introduce the loopback / LAN reachability the PRD
        # design carefully avoids.
        cfg = self._build(bundle_ip="10.20.30.40")
        self.assertEqual(
            {"allow_cidrs": ["10.20.30.40/32"]},
            cfg["network"],
        )

    def test_no_image_or_command_emitted(self):
        # The chunk-1 renderer (under the abandoned gvproxy design)
        # emitted `name = ...` + `[[net]] attachment="unixgram"`.
        # The new renderer carries only the per-bottle overrides;
        # image / entrypoint / cmd come from the .smolmachine
        # artifact, not the Smolfile.
        cfg = self._build()
        self.assertNotIn("image", cfg)
        self.assertNotIn("entrypoint", cfg)
        self.assertNotIn("cmd", cfg)
        self.assertNotIn("command", cfg)
        self.assertNotIn("name", cfg)


class TestSmolfileRender(unittest.TestCase):
    def _render(self, **kwargs):
        defaults = dict(
            env={"HTTPS_PROXY": "http://192.168.50.2:8888"},
            bundle_ip="192.168.50.2",
        )
        defaults.update(kwargs)
        return smolfile_render(smolfile_build(**defaults))

    def test_round_trip_through_tomllib(self):
        import tomllib  # stdlib in 3.11+
        rendered = self._render()
        parsed = tomllib.loads(rendered)
        self.assertIn(
            "HTTPS_PROXY=http://192.168.50.2:8888",
            parsed["env"],
        )
        self.assertEqual(
            ["192.168.50.2/32"],
            parsed["network"]["allow_cidrs"],
        )

    def test_no_tsi_outbound_localhost_only(self):
        # Whole point of the design pivot: never emit
        # `--outbound-localhost-only` or similar that would
        # re-open host loopback.
        text = self._render()
        self.assertNotIn("outbound_localhost_only", text)
        self.assertNotIn("outbound-localhost-only", text)
        # And no gvproxy / virtio-net carve-out leaked from the
        # abandoned first draft.
        self.assertNotIn("unixgram", text)
        self.assertNotIn("gvproxy", text.lower())

    def test_special_chars_in_env_value_escape(self):
        import tomllib
        cfg = smolfile_build(
            env={"WITH_QUOTES": 'has "double" quotes'},
            bundle_ip="10.0.0.1",
        )
        rendered = smolfile_render(cfg)
        parsed = tomllib.loads(rendered)
        self.assertIn('WITH_QUOTES=has "double" quotes', parsed["env"])


if __name__ == "__main__":
    unittest.main()
