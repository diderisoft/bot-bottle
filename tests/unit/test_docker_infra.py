"""Unit: DockerInfraService composes the orchestrator + gateway services.

`DockerInfraService` no longer owns the container lifecycles — it composes the
`DockerOrchestrator` (control plane) and `DockerGateway` (data plane) services,
each tested in its own module. These tests isolate the *composition*: both are
built, the orchestrator comes up first, then the gateway is connected to it with
a freshly minted token."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from bot_bottle.backend.docker.infra import (
    ORCHESTRATOR_NAME,
    DockerInfraService,
)
from bot_bottle.gateway import GATEWAY_NAME

_RUN = "bot_bottle.backend.docker.infra.run_docker"


class TestDockerInfraService(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = DockerInfraService(port=8099)
        self.orch = MagicMock()
        self.orch.url.return_value = "http://127.0.0.1:8099"
        self.orch.gateway_url.return_value = f"http://{ORCHESTRATOR_NAME}:8099"
        self.orch.mint_gateway_token.return_value = "gw.jwt"
        self.gw = MagicMock()
        for name, mock in (("orchestrator", self.orch), ("gateway", self.gw)):
            p = patch.object(self.svc, name, return_value=mock)
            p.start()
            self.addCleanup(p.stop)

    def test_gateway_name(self) -> None:
        self.assertEqual(GATEWAY_NAME, self.svc.gateway_name)

    def test_url_delegates_to_the_orchestrator(self) -> None:
        self.assertEqual("http://127.0.0.1:8099", self.svc.url())

    def test_is_healthy_delegates_to_the_orchestrator(self) -> None:
        self.orch.is_healthy.return_value = True
        self.assertTrue(self.svc.is_healthy())
        self.orch.is_healthy.assert_called_once()

    def test_ensure_running_builds_both_then_starts_orchestrator_first(self) -> None:
        url = self.svc.ensure_running()
        self.assertEqual("http://127.0.0.1:8099", url)
        # Both images built; the control plane comes up before the gateway.
        self.orch.ensure_built.assert_called_once()
        self.gw.ensure_built.assert_called_once()
        self.orch.ensure_running.assert_called_once()

    def test_ensure_running_connects_gateway_with_minted_token(self) -> None:
        self.svc.ensure_running()
        # The gateway is bound to the orchestrator's *gateway_url* (the control
        # DNS name), carrying the orchestrator-minted `gateway` token.
        self.gw.connect_to_orchestrator.assert_called_once_with(
            f"http://{ORCHESTRATOR_NAME}:8099", "gw.jwt")
        self.orch.mint_gateway_token.assert_called_once()

    def test_stop_removes_both_containers(self) -> None:
        with patch(_RUN) as run:
            self.svc.stop()
        rms = [
            c.args[0] for c in run.call_args_list
            if c.args[0][:3] == ["docker", "rm", "--force"]
        ]
        self.assertTrue(any(GATEWAY_NAME in a for a in rms))
        self.assertTrue(any(ORCHESTRATOR_NAME in a for a in rms))


if __name__ == "__main__":
    unittest.main()
