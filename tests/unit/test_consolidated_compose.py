"""Unit: agent-only consolidated compose render (PRD 0070)."""

from __future__ import annotations

import unittest

from bot_bottle.backend.docker.consolidated_compose import consolidated_agent_compose
from tests.unit._docker_bottle_plan import _plan

_GW = "172.18.0.2"
_IP = "172.18.0.5"
_NET = "bot-bottle-gateway"


class TestConsolidatedAgentCompose(unittest.TestCase):
    def _spec(self, *, runsc: bool = False):
        plan = _plan(with_egress=True, supervise=True, with_git=True)
        if runsc:
            plan = type(plan)(**{**vars(plan), "use_runsc": True})  # type: ignore[arg-type]
        return consolidated_agent_compose(plan, gateway_ip=_GW, source_ip=_IP, network=_NET)

    def test_only_agent_service_no_companion_container(self) -> None:
        # The whole point of consolidation: no per-bottle gateway.
        self.assertEqual(["agent"], list(self._spec()["services"]))

    def test_agent_pinned_on_external_gateway_network(self) -> None:
        spec = self._spec()
        self.assertEqual({"external": True}, spec["networks"][_NET])
        agent_net = spec["services"]["agent"]["networks"][_NET]
        self.assertEqual(_IP, agent_net["ipv4_address"])

    def test_proxy_and_ca_point_at_gateway(self) -> None:
        env = self._spec()["services"]["agent"]["environment"]
        self.assertIn(f"HTTPS_PROXY=http://{_GW}:9099", env)
        # git-http + supervise on the gateway must bypass the egress proxy.
        self.assertTrue(any(e.startswith("NO_PROXY=") and _GW in e for e in env))

    def test_no_companion_container_dependency(self) -> None:
        self.assertNotIn("depends_on", self._spec()["services"]["agent"])

    def test_runsc_runtime_when_enabled(self) -> None:
        self.assertEqual("runsc", self._spec(runsc=True)["services"]["agent"]["runtime"])

    def test_forwarded_env_stays_bare_names(self) -> None:
        env = self._spec()["services"]["agent"]["environment"]
        # forwarded secrets are bare names (value inherited from process env).
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_env_var_secret_stays_a_bare_name(self) -> None:
        plan = _plan(with_egress=True, supervise=True, with_git=True)
        plan = type(plan)(**{**vars(plan), "env_var_secret": "secret-value"})
        env = consolidated_agent_compose(
            plan, gateway_ip=_GW, source_ip=_IP, network=_NET,
        )["services"]["agent"]["environment"]
        self.assertIn("ENV_VAR_SECRET", env)
        self.assertNotIn("ENV_VAR_SECRET=secret-value", env)


if __name__ == "__main__":
    unittest.main()
