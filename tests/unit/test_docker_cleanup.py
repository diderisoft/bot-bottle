"""Unit: backend/docker/cleanup's enumeration fails loudly.

State dirs are reaped for every backend in backend/state_cleanup
(tests/unit/test_state_cleanup.py). The compose-project + container/network
enumeration is left to the integration tests because it requires a real
docker daemon."""

from __future__ import annotations

import unittest
from collections.abc import Callable
from unittest.mock import patch

from bot_bottle.contract.hostctl import Settings
from bot_bottle.backend import EnumerationError
from bot_bottle.backend.docker import cleanup
from bot_bottle.backend.docker.infra import INFRA_CONTAINERS, INFRA_NETWORKS
from bot_bottle.environment import resource_prefix


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


class TestTheInfraStays(unittest.TestCase):
    """The gateway, orchestrator and their networks share the prefix but
    aren't bottles: cleaning up bots leaves them running."""

    def _listed(self, names: frozenset[str], lister: Callable[[], list[str]]) -> None:
        stray = resource_prefix() + "stray"
        out = "".join(f"{n}\t\n" for n in [*sorted(names), stray])
        completed = cleanup.subprocess.CompletedProcess([], 0, stdout=out, stderr="")
        with patch.object(cleanup.subprocess, "run", return_value=completed):
            self.assertEqual([stray], lister())

    def test_infra_containers_stay(self):
        self._listed(INFRA_CONTAINERS, cleanup._list_prefixed_containers)

    def test_infra_networks_stay(self):
        self._listed(INFRA_NETWORKS, cleanup._list_prefixed_networks)


if __name__ == "__main__":
    unittest.main()
