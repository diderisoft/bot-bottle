"""Unit: smolmachines backend util helpers (PRD 0023)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from bot_bottle.backend.smolmachines.util import (
    smolmachines_bundle_subnet,
    smolmachines_preflight,
)


class TestBundleSubnet(unittest.TestCase):
    def test_returns_subnet_gateway_and_bundle_ip(self):
        subnet, gateway, bundle_ip = smolmachines_bundle_subnet("demo-abc12")
        self.assertTrue(subnet.startswith("192.168."))
        self.assertTrue(subnet.endswith(".0/24"))
        # Gateway at .1, bundle at .2 — fixed convention.
        self.assertTrue(gateway.endswith(".1"))
        self.assertTrue(bundle_ip.endswith(".2"))
        # All three share the same third octet.
        third = subnet.split(".")[2]
        self.assertEqual(third, gateway.split(".")[2])
        self.assertEqual(third, bundle_ip.split(".")[2])

    def test_stable_for_same_slug(self):
        # Recoverability: `cli.py resume` reuses the slug and
        # expects to find the same per-bottle subnet (a fresh
        # docker bridge would mean a different IP, and smolvm's
        # allow_cidrs would no longer match).
        a = smolmachines_bundle_subnet("demo-abc12")
        b = smolmachines_bundle_subnet("demo-abc12")
        self.assertEqual(a, b)

    def test_different_slugs_likely_differ(self):
        # Not a guarantee — it's hash-mod-254, collisions exist —
        # but two arbitrary slugs shouldn't share a subnet in the
        # typical case.
        seen = {
            smolmachines_bundle_subnet(s)[0]
            for s in ("a", "b", "c", "d", "e", "alpha", "beta", "gamma")
        }
        self.assertGreater(len(seen), 1)

    def test_skips_docker_default_octet(self):
        # docker's default bridge sits at 172.17.x.x; operators
        # often also see 192.168.17.x from VPN clients on macOS.
        # The util skips octet 17 → 18 so the smolmachines subnet
        # doesn't collide with that historical pain point.
        for slug in (f"slug-{i}" for i in range(500)):
            subnet, _, _ = smolmachines_bundle_subnet(slug)
            self.assertNotEqual("192.168.17.0/24", subnet,
                                f"slug {slug!r} landed on the skipped octet")


class TestPreflight(unittest.TestCase):
    def test_smolvm_present_returns_none(self):
        with patch(
            "bot_bottle.backend.smolmachines.util.shutil.which",
            return_value="/usr/local/bin/smolvm",
        ):
            self.assertIsNone(smolmachines_preflight())

    def test_missing_smolvm_dies(self):
        with patch(
            "bot_bottle.backend.smolmachines.util.shutil.which",
            return_value=None,
        ):
            with self.assertRaises(SystemExit) as cm:
                smolmachines_preflight()
            self.assertNotEqual(0, cm.exception.code)

    def test_install_pointer_in_error(self):
        import io
        import sys
        with patch(
            "bot_bottle.backend.smolmachines.util.shutil.which",
            return_value=None,
        ):
            captured = io.StringIO()
            with patch.object(sys, "stderr", captured):
                with self.assertRaises(SystemExit):
                    smolmachines_preflight()
            msg = captured.getvalue()
            self.assertIn("smolvm", msg)
            self.assertIn("smolmachines.com/install.sh", msg)


if __name__ == "__main__":
    unittest.main()
