"""Unit: supervise MCP provisioning (PRD 0013 follow-up).

The real provisioning runs `claude mcp add` inside the agent
container — exercised by the existing supervise integration test
chain once the agent container is brought up. Here we just cover
the URL computation so a regression in SUPERVISE_HOSTNAME / PORT
plumbing surfaces in unit CI."""

import unittest

from claude_bottle.backend.docker.provision.supervise import supervise_mcp_url
from claude_bottle.supervise import SUPERVISE_HOSTNAME, SUPERVISE_PORT


class TestSuperviseMcpUrl(unittest.TestCase):
    def test_url_matches_sidecar_constants(self):
        self.assertEqual(
            f"http://{SUPERVISE_HOSTNAME}:{SUPERVISE_PORT}/",
            supervise_mcp_url(),
        )

    def test_url_is_http_not_https(self):
        # The agent dials the sidecar on the internal docker network;
        # no TLS termination, no CA trust juggling. If this ever
        # needs HTTPS, the sidecar's listener side has to change too.
        self.assertTrue(supervise_mcp_url().startswith("http://"))


if __name__ == "__main__":
    unittest.main()
