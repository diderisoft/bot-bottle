"""Tests for the socket-shared Docker host address helper."""

from __future__ import annotations

import unittest

from scripts.docker_host_address import default_ipv4_gateway


class TestDefaultIpv4Gateway(unittest.TestCase):
    def test_decodes_linux_little_endian_gateway(self) -> None:
        routes = (
            "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
            "eth0 00000000 010011AC 0003 0 0 0 00000000 0 0 0\n"
        )
        self.assertEqual("172.17.0.1", default_ipv4_gateway(routes))

    def test_rejects_table_without_default_gateway(self) -> None:
        routes = (
            "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
            "eth0 000011AC 00000000 0001 0 0 0 00FFFFFF 0 0 0\n"
        )
        with self.assertRaisesRegex(ValueError, "no active"):
            default_ipv4_gateway(routes)


if __name__ == "__main__":
    unittest.main()
