"""Unit: backend/docker/cleanup's enumeration fails loudly.

State dirs are reaped for every backend in backend/state_cleanup
(tests/unit/test_state_cleanup.py). The compose-project + container/network
enumeration is left to the integration tests because it requires a real
docker daemon."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from bot_bottle.contract.hostctl import Settings
from bot_bottle.backend import EnumerationError
from bot_bottle.backend.docker import cleanup


class TestAuthoritativeDiscovery(unittest.TestCase):
    def test_prepare_requires_authoritative_compose_projects(self):
        with patch.object(cleanup.docker_mod, "require_docker"), \
             patch.object(
                 cleanup, "list_compose_projects",
                 side_effect=EnumerationError("compose unavailable"),
             ) as projects, self.assertRaisesRegex(
                 EnumerationError, "compose unavailable",
             ):
            cleanup.prepare_cleanup(Settings())
        projects.assert_called_once_with(
            warn_on_error=False,
            raise_on_error=True,
        )

    def test_container_query_failure_raises(self):
        failed = cleanup.subprocess.CompletedProcess(
            [], 1, stdout="", stderr="daemon unavailable",
        )
        with patch.object(cleanup.subprocess, "run", return_value=failed), \
             self.assertRaisesRegex(EnumerationError, "daemon unavailable"):
            cleanup._list_prefixed_containers()

    def test_network_query_failure_raises(self):
        failed = cleanup.subprocess.CompletedProcess(
            [], 1, stdout="", stderr="daemon unavailable",
        )
        with patch.object(cleanup.subprocess, "run", return_value=failed), \
             self.assertRaisesRegex(EnumerationError, "daemon unavailable"):
            cleanup._list_prefixed_networks()


if __name__ == "__main__":
    unittest.main()
