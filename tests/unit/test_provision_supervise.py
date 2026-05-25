"""Unit: supervise MCP settings renderer (PRD 0013 follow-up).

The docker cp / chown side of provision_supervise is exercised by
the existing supervise integration test once the agent container is
brought up; here we cover the pure render path so a settings.json
shape regression would surface in unit-level CI."""

import json
import unittest

from claude_bottle.backend.docker.provision.supervise import render_settings
from claude_bottle.supervise import SUPERVISE_HOSTNAME, SUPERVISE_PORT


class TestRenderSettings(unittest.TestCase):
    def test_output_is_valid_json(self):
        json.loads(render_settings())

    def test_has_mcp_servers_supervise_http_entry(self):
        cfg = json.loads(render_settings())
        servers = cfg["mcpServers"]
        self.assertIn("supervise", servers)
        sv = servers["supervise"]
        self.assertEqual("http", sv["type"])
        self.assertEqual(
            f"http://{SUPERVISE_HOSTNAME}:{SUPERVISE_PORT}/",
            sv["url"],
        )

    def test_only_supervise_server_is_emitted(self):
        cfg = json.loads(render_settings())
        # Keep the provisioner narrowly scoped — it owns just the
        # supervise entry, no other tools/servers.
        self.assertEqual({"supervise"}, set(cfg["mcpServers"].keys()))


if __name__ == "__main__":
    unittest.main()
