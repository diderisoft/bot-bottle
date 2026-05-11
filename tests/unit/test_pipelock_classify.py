"""Unit: is_ipv4_literal — the classifier that decides whether
bottle.ssh[].Hostname goes into pipelock's ssrf.ip_allowlist (IPv4
literal) or trusted_domains (hostname)."""

import unittest

from claude_bottle.util import is_ipv4_literal


class TestIPv4Classify(unittest.TestCase):
    def test_positive(self):
        for ip in ("127.0.0.1", "10.0.0.5", "192.0.2.10", "0.0.0.0", "255.255.255.255"):
            with self.subTest(ip=ip):
                self.assertTrue(is_ipv4_literal(ip), ip)

    def test_negative(self):
        for hn in (
            "github.com",
            "gitea.dideric.is",
            "100.78.141",
            "192.0.2.10.5",
            "::1",
            "fe80::1",
            "localhost",
            "",
            "1.2.3.4.example.com",
        ):
            with self.subTest(hn=hn):
                self.assertFalse(is_ipv4_literal(hn), hn)


if __name__ == "__main__":
    unittest.main()
