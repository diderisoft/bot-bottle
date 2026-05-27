"""Unit: smolmachines backend util helpers (PRD 0023 chunk 1)."""

from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

from claude_bottle.backend.smolmachines.util import (
    allocate_loopback_port,
    smolmachines_gvproxy_subnet,
    smolmachines_preflight,
)


class TestGvproxySubnet(unittest.TestCase):
    def test_returns_192_168_X_format(self):
        subnet, gateway = smolmachines_gvproxy_subnet("demo-abc12")
        self.assertTrue(subnet.startswith("192.168."))
        self.assertTrue(subnet.endswith(".0/24"))
        self.assertTrue(gateway.startswith("192.168."))
        self.assertTrue(gateway.endswith(".1"))
        # The subnet and gateway share the same third octet.
        sub_octet = subnet.split(".")[2]
        gw_octet = gateway.split(".")[2]
        self.assertEqual(sub_octet, gw_octet)

    def test_stable_for_same_slug(self):
        # Recoverability: `resume` reuses the slug + expects the
        # same subnet so a re-attach doesn't try to grab a fresh
        # network range from gvproxy.
        a = smolmachines_gvproxy_subnet("demo-abc12")
        b = smolmachines_gvproxy_subnet("demo-abc12")
        self.assertEqual(a, b)

    def test_different_slugs_likely_differ(self):
        # Not a guarantee (it's hash-mod-254 so collisions exist),
        # but two arbitrary slugs shouldn't share a subnet in the
        # typical case.
        seen = {
            smolmachines_gvproxy_subnet(s)
            for s in ("a", "b", "c", "d", "e", "alpha", "beta", "gamma")
        }
        self.assertGreater(len(seen), 1)

    def test_never_collides_with_docker_default_bridge(self):
        # docker's default bridge sits at 172.17.x.x but operators
        # commonly also see 192.168.17.x from VPN clients on macOS.
        # The util explicitly skips octet 17 → 18 so the smolmachines
        # subnet doesn't collide with that historical pain point.
        for slug in (f"slug-{i}" for i in range(500)):
            subnet, gateway = smolmachines_gvproxy_subnet(slug)
            self.assertNotEqual("192.168.17.0/24", subnet,
                                f"slug {slug!r} landed on the skipped octet")


class TestAllocateLoopbackPort(unittest.TestCase):
    def test_returns_in_ephemeral_range(self):
        port = allocate_loopback_port()
        # Linux ephemeral starts at 32768; macOS at 49152. Either
        # way it's >1024, which is what matters.
        self.assertGreater(port, 1024)
        self.assertLess(port, 65536)

    def test_port_is_free_at_return(self):
        # The dance is bind-with-port-0 + getsockname + close. By
        # the time we return, the kernel has the port back in the
        # free pool. We confirm by binding it ourselves immediately
        # (we'll race with anyone else who races for it; the
        # race-window caveat lives in the docstring).
        port = allocate_loopback_port()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
        finally:
            s.close()

    def test_multiple_calls_return_distinct_ports(self):
        # The kernel rotates ephemeral ports; consecutive calls
        # almost certainly land different ports.
        ports = {allocate_loopback_port() for _ in range(8)}
        self.assertGreater(len(ports), 1)


class TestPreflight(unittest.TestCase):
    def test_both_binaries_present_returns_none(self):
        with patch(
            "claude_bottle.backend.smolmachines.util.shutil.which",
            side_effect=lambda name: f"/usr/local/bin/{name}",
        ):
            self.assertIsNone(smolmachines_preflight())

    def test_missing_smolvm_dies_with_pointer(self):
        with patch(
            "claude_bottle.backend.smolmachines.util.shutil.which",
            side_effect=lambda name: None if name == "smolvm" else f"/x/{name}",
        ):
            with self.assertRaises(SystemExit) as cm:
                smolmachines_preflight()
            self.assertNotEqual(0, cm.exception.code)

    def test_missing_gvproxy_dies_with_pointer(self):
        with patch(
            "claude_bottle.backend.smolmachines.util.shutil.which",
            side_effect=lambda name: None if name == "gvproxy" else f"/x/{name}",
        ):
            with self.assertRaises(SystemExit):
                smolmachines_preflight()

    def test_missing_both_lists_both_in_message(self):
        # When both are gone, the message names both binaries and
        # gives both install pointers — operator shouldn't have to
        # re-run to discover the second missing dep.
        import io, sys
        with patch(
            "claude_bottle.backend.smolmachines.util.shutil.which",
            return_value=None,
        ):
            captured = io.StringIO()
            with patch.object(sys, "stderr", captured):
                with self.assertRaises(SystemExit):
                    smolmachines_preflight()
            msg = captured.getvalue()
            self.assertIn("smolvm", msg)
            self.assertIn("gvproxy", msg)


if __name__ == "__main__":
    unittest.main()
