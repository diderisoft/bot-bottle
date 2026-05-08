"""Unit: pipelock naming helpers (container_name, proxy_url, proxy_host_port)."""

import unittest

from claude_bottle.pipelock import (
    pipelock_container_name,
    pipelock_proxy_host_port,
    pipelock_proxy_url,
)


class TestPipelockNaming(unittest.TestCase):
    def test_container_name_simple(self):
        self.assertEqual("claude-bottle-pipelock-foo", pipelock_container_name("foo"))

    def test_container_name_with_hyphens(self):
        self.assertEqual(
            "claude-bottle-pipelock-some-slug", pipelock_container_name("some-slug")
        )

    def test_proxy_url_default_port(self):
        self.assertEqual(
            "http://claude-bottle-pipelock-foo:8888", pipelock_proxy_url("foo")
        )

    def test_proxy_host_port_default_port(self):
        self.assertEqual(
            "claude-bottle-pipelock-foo:8888", pipelock_proxy_host_port("foo")
        )


if __name__ == "__main__":
    unittest.main()
