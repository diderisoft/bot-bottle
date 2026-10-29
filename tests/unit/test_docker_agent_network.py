"""Unit: per-agent docker networks (#810): subnet allocation, creation that
survives a concurrent launch, the gateway join, and teardown."""

from __future__ import annotations

import contextlib
import io
import subprocess
import unittest
from unittest.mock import patch

from bot_bottle.backend.docker import agent_network as an
from bot_bottle.backend.docker import cleanup
from bot_bottle.backend.docker.bottle_cleanup_plan import DockerBottleCleanupPlan
from bot_bottle.backend.docker.gateway_net import (
    AGENT_SUBNET_POOL, NoFreeAddressError, agent_subnet_addresses, free_agent_subnets,
)
from bot_bottle.contract.hostctl import Settings
from bot_bottle.control.gateway import GATEWAY_NAME


def _done(rc: int = 0, out: str = "", err: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess([], rc, out, err)


class TestSubnetAllocation(unittest.TestCase):
    def test_lowest_free_29s_skip_every_overlapping_network(self) -> None:
        taken = ["10.242.0.0/29", "10.242.0.16/28", "172.17.0.0/16", "fd00::/64"]
        first = list(zip(range(3), free_agent_subnets(taken)))
        self.assertEqual(["10.242.0.8/29", "10.242.0.32/29", "10.242.0.40/29"],
                         [s for _, s in first])

    def test_a_network_covering_the_pool_leaves_nothing(self) -> None:
        self.assertEqual([], list(free_agent_subnets(["10.242.0.0/16"])))

    def test_the_pool_holds_512_agents_apart_from_the_shared_network(self) -> None:
        subnets = list(free_agent_subnets(["10.242.255.0/24"]))
        self.assertEqual(512, len(subnets))
        self.assertTrue(all(s.startswith("10.242.") for s in subnets))
        self.assertEqual("10.242.0.0/20", AGENT_SUBNET_POOL)

    def test_gateway_is_dot_2_and_agent_dot_3(self) -> None:
        self.assertEqual(("10.242.0.10", "10.242.0.11"), agent_subnet_addresses("10.242.0.8/29"))


class TestCreate(unittest.TestCase):
    def _run(self, creates: list["subprocess.CompletedProcess[str]"], *,
             existing: str = "10.242.0.0/29", inspect: str = "1|true|10.242.0.0/29"):
        calls: list[list[str]] = []
        answers = iter(creates)

        def run(argv: list[str], **_: object) -> "subprocess.CompletedProcess[str]":
            calls.append(argv)
            if argv[:3] == ["docker", "network", "ls"]:
                return _done(out="id1\n")
            if argv[:3] == ["docker", "network", "inspect"] and argv[-1] == "id1":
                return _done(out=f"{existing} \n")
            if argv[:3] == ["docker", "network", "inspect"]:
                return _done(out=inspect)
            return next(answers)
        with patch.object(an, "run_docker", side_effect=run):
            return an.create_agent_network("bot-bottle-agent-x"), calls

    def test_creates_an_internal_ipv4_network_on_the_first_free_subnet(self) -> None:
        subnet, calls = self._run([_done()])
        self.assertEqual("10.242.0.8/29", subnet)
        create = [c for c in calls if c[:3] == ["docker", "network", "create"]]
        self.assertEqual([[
            "docker", "network", "create", "--internal", "--ipv6=false",
            "--subnet", "10.242.0.8/29", "--label", "bot-bottle.agent-network=1",
            "bot-bottle-agent-x",
        ]], create)

    def test_a_subnet_a_concurrent_launch_took_moves_on_to_the_next(self) -> None:
        overlap = _done(1, err="invalid pool request: Pool overlaps with other one")
        subnet, _ = self._run([overlap, _done()])
        self.assertEqual("10.242.0.16/29", subnet)

    def test_a_crashed_launchs_network_is_reused(self) -> None:
        exists = _done(1, err="network with name bot-bottle-agent-x already exists")
        subnet, _ = self._run([exists], inspect="1|true|10.242.0.24/29")
        self.assertEqual("10.242.0.24/29", subnet)

    def test_a_same_named_network_that_isnt_ours_or_internal_is_refused(self) -> None:
        exists = _done(1, err="already exists")
        for inspect in ("1|false|10.242.0.24/29", "|true|10.242.0.24/29"):
            with self.subTest(inspect=inspect), self.assertRaises(an.AgentNetworkError):
                self._run([exists], inspect=inspect)

    def test_other_create_failures_raise(self) -> None:
        with self.assertRaisesRegex(an.AgentNetworkError, "daemon gone"):
            self._run([_done(1, err="daemon gone")])

    def test_an_exhausted_pool_raises(self) -> None:
        with self.assertRaises(NoFreeAddressError):
            self._run([], existing="10.242.0.0/16")


class TestGatewayAndTeardown(unittest.TestCase):
    def test_connect_pins_the_gateway_address_and_is_idempotent(self) -> None:
        for answer in (_done(), _done(1, err="endpoint with name gw already exists in network")):
            with patch.object(an, "run_docker", return_value=answer) as run:
                an.connect_gateway("bot-bottle-agent-x", "gw", "10.242.0.10")
            run.assert_called_once_with(
                ["docker", "network", "connect", "--ip", "10.242.0.10", "bot-bottle-agent-x", "gw"])

    def test_connect_failure_raises(self) -> None:
        with patch.object(an, "run_docker", return_value=_done(1, err="boom")), \
                self.assertRaisesRegex(an.AgentNetworkError, "boom"):
            an.connect_gateway("bot-bottle-agent-x", "gw", "10.242.0.10")

    def test_remove_detaches_the_gateway_first(self) -> None:
        with patch.object(an, "run_docker", return_value=_done()) as run:
            an.remove_agent_network("bot-bottle-agent-x", "gw")
        self.assertEqual(
            [["docker", "network", "disconnect", "--force", "bot-bottle-agent-x", "gw"],
             ["docker", "network", "rm", "bot-bottle-agent-x"]],
            [c.args[0] for c in run.call_args_list])

    def test_remove_never_touches_a_network_that_isnt_per_agent(self) -> None:
        with patch.object(an, "run_docker") as run:
            an.remove_agent_network("bot-bottle-gateway", "gw")
        run.assert_not_called()

    def test_remove_warns_but_never_raises(self) -> None:
        err = io.StringIO()
        with patch.object(an, "run_docker", return_value=_done(1, err="has active endpoints")), \
                contextlib.redirect_stderr(err):
            an.remove_agent_network("bot-bottle-agent-x", "gw")
        self.assertIn("could not remove agent network bot-bottle-agent-x", err.getvalue())

    def test_an_already_removed_network_is_quiet(self) -> None:
        err = io.StringIO()
        with patch.object(an, "run_docker",
                          return_value=_done(1, err="network bot-bottle-agent-x not found")), \
                contextlib.redirect_stderr(err):
            an.remove_agent_network("bot-bottle-agent-x", "gw")
        self.assertEqual("", err.getvalue())


class TestCleanup(unittest.TestCase):
    def test_a_stray_agent_network_is_detached_from_the_gateway_before_removal(self) -> None:
        plan = DockerBottleCleanupPlan(
            projects=(), stray_containers=(),
            stray_networks=("bot-bottle-agent-x", "bot-bottle-other"))
        order: list[list[str]] = []

        def record(argv: list[str], **_: object) -> "subprocess.CompletedProcess[str]":
            order.append(argv)
            return _done()
        with patch.object(cleanup.docker_mod, "run_docker", side_effect=record), \
                patch("bot_bottle.backend.cleanup_control.subprocess.run", side_effect=record), \
                contextlib.redirect_stderr(io.StringIO()):
            cleanup.cleanup(plan, Settings())
        self.assertEqual([
            ["docker", "network", "disconnect", "--force", "bot-bottle-agent-x",
             GATEWAY_NAME],
            ["docker", "network", "rm", "bot-bottle-agent-x"],
            ["docker", "network", "rm", "bot-bottle-other"],
        ], order)


if __name__ == "__main__":
    unittest.main()
