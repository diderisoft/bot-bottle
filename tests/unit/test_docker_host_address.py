"""Tests for the socket-shared Docker host address helper."""

from __future__ import annotations

import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from scripts.docker_host_address import default_ipv4_gateway, main


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

    def test_ignores_short_malformed_and_inactive_routes(self) -> None:
        routes = (
            "Iface Destination Gateway Flags\n"
            "short row\n"
            "eth0 00000000 nope 0003\n"
            "eth0 00000000 010011AC 0001\n"
        )
        with self.assertRaisesRegex(ValueError, "no active"):
            default_ipv4_gateway(routes)

    def test_main_prints_gateway(self) -> None:
        routes = (
            "Iface Destination Gateway Flags\n"
            "eth0 00000000 010011AC 0003\n"
        )
        output = StringIO()
        with patch("pathlib.Path.read_text", return_value=routes), \
                redirect_stdout(output):
            self.assertEqual(0, main())
        self.assertEqual("172.17.0.1\n", output.getvalue())

    def test_main_reports_route_error(self) -> None:
        error = StringIO()
        with patch("pathlib.Path.read_text", side_effect=OSError("denied")), \
                redirect_stderr(error):
            self.assertEqual(1, main())
        self.assertIn("docker-host-address: denied", error.getvalue())


if __name__ == "__main__":
    unittest.main()
