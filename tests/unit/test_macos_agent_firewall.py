"""Unit: the firewall in each macOS agent's VM (#810).

Agents share one vmnet network and pf can't see traffic between them, so each
agent's own kernel allows only the gateway. These pin the ruleset, how its
load is reported, the root check, and that reattach reloads it for a moved
gateway.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from bot_bottle.backend.gateway_attach import GatewayAttachError, RunningBottle
from bot_bottle.backend.macos_container import agent_firewall
from bot_bottle.backend.macos_container.gateway_attach import MacosGatewayAttacher
from bot_bottle.backend.macos_container.gateway_hosts import GATEWAY_HOSTNAME
from bot_bottle.backend.util import ROOT_EXEC_PATH

_A = RunningBottle(ref="bot-bottle-a", slug="a", source_ip="192.168.128.9")


class TestRuleset(unittest.TestCase):
    def test_only_the_gateway_is_allowed_out_and_nothing_new_in(self) -> None:
        rules = agent_firewall.ruleset("192.168.128.2")
        output, inbound = rules.split("chain input")
        self.assertIn("hook output priority 0; policy drop;", output)
        self.assertIn("ip daddr 192.168.128.2 accept", output)
        self.assertIn("ct state established,related accept", output)
        self.assertIn("hook input priority 0; policy drop;", inbound)
        self.assertNotIn("daddr", inbound)
        self.assertNotIn("saddr", rules)

    def test_the_rest_fails_at_once_but_not_as_a_refusal(self) -> None:
        # A refusal is what the isolation probe takes for a sibling answering.
        output = agent_firewall.ruleset("192.168.128.2").split("chain input", maxsplit=1)[0]
        self.assertIn("reject with icmpx admin-prohibited", output)
        self.assertNotIn("tcp reset", output)

    def test_root_is_exempt_so_the_probe_can_test_pf(self) -> None:
        self.assertIn("meta skuid 0 accept", agent_firewall.ruleset("192.168.128.2"))

    def test_covers_both_address_families(self) -> None:
        self.assertIn(f"table inet {agent_firewall.TABLE} {{",
                      agent_firewall.ruleset("192.168.128.2"))
        self.assertIn("ip6 daddr fd00::2 accept", agent_firewall.ruleset("fd00::2"))

    def test_reloading_replaces_the_table(self) -> None:
        head = agent_firewall.ruleset("192.168.128.2").splitlines()[:2]
        self.assertEqual([f"table inet {agent_firewall.TABLE}",
                          f"delete table inet {agent_firewall.TABLE}"], head)

    def test_a_non_ip_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            agent_firewall.ruleset("10.0.0.1; rm -rf /")


class TestScript(unittest.TestCase):
    def test_runs_with_a_pinned_path(self) -> None:
        self.assertTrue(agent_firewall.firewall_script("192.168.128.2")
                        .startswith(f"export PATH={ROOT_EXEC_PATH};"))

    def test_points_at_the_gateway_and_loads_the_firewall_in_one_root_shell(self) -> None:
        argv = agent_firewall.point_at_gateway_argv("192.168.128.2")
        self.assertEqual(["/bin/sh", "-c"], argv[:2])
        self.assertIn(GATEWAY_HOSTNAME, argv[2])
        self.assertLess(argv[2].index("/etc/hosts"), argv[2].index("nft -f -"))

    def test_the_ruleset_survives_the_shell_quoting(self) -> None:
        # The script pipes the ruleset through `printf '%s' '...'`; run that
        # part with `nft` stubbed by `cat` and compare.
        script = agent_firewall.firewall_script("192.168.128.2")
        body = script.split("if err=$(", 1)[1].split(" | nft", 1)[0]
        out = subprocess.run(["/bin/sh", "-c", body], capture_output=True, text=True,
                             check=True).stdout
        self.assertEqual(agent_firewall.ruleset("192.168.128.2"), out)


class TestFirewallLoaded(unittest.TestCase):
    def test_ok(self) -> None:
        self.assertTrue(agent_firewall.firewall_loaded("bb-firewall=ok\n"))

    def test_an_image_without_nft_is_not_an_error(self) -> None:
        self.assertFalse(agent_firewall.firewall_loaded("bb-firewall=no-nft\n"))

    def test_a_refused_ruleset_raises_with_nfts_error(self) -> None:
        with self.assertRaises(ValueError) as cm:
            agent_firewall.firewall_loaded(
                "bb-firewall=failed\nOperation not permitted\n")
        self.assertIn("Operation not permitted", str(cm.exception))

    def test_no_status_raises(self) -> None:
        with self.assertRaises(ValueError):
            agent_firewall.firewall_loaded("")

    def test_point_at_gateway_runs_as_root(self) -> None:
        with patch.object(agent_firewall.container_mod, "exec_container_as_root",
                          return_value="bb-firewall=ok\n") as ex:
            self.assertTrue(agent_firewall.point_at_gateway("bot-bottle-a", "192.168.128.2"))
        self.assertEqual(("bot-bottle-a", agent_firewall.point_at_gateway_argv("192.168.128.2")),
                         ex.call_args.args)


class TestPrivileges(unittest.TestCase):
    def test_each_way_to_root_is_reported(self) -> None:
        out = ("noise\nbb-privilege=passwordless sudo\n"
               "bb-privilege=file capabilities on /usr/bin/x\n")
        self.assertEqual(["passwordless sudo", "file capabilities on /usr/bin/x"],
                         agent_firewall.privileges(out))

    def test_none(self) -> None:
        self.assertEqual([], agent_firewall.privileges(""))

    def test_the_check_runs_here_and_finds_nothing_for_this_user(self) -> None:
        # Runs on the test host as an ordinary user: it must at least parse
        # and exit zero.
        result = subprocess.run(["/bin/sh", "-c", agent_firewall.privilege_check_script()],
                                capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode)


class TestReattach(unittest.TestCase):
    def _done(self, out: str, code: int = 0) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], code, out, "")

    def test_a_moved_gateway_reloads_the_firewall(self) -> None:
        with patch("bot_bottle.backend.macos_container.gateway_attach.container_mod"
                   ".run_container_argv", return_value=self._done("bb-firewall=ok\n")) as run:
            MacosGatewayAttacher("net").point_at_gateway(_A, "192.168.128.3")
        argv = run.call_args.args[0]
        self.assertEqual(["container", "exec", "--user", "root", "bot-bottle-a"], argv[:5])
        self.assertEqual(agent_firewall.point_at_gateway_argv("192.168.128.3"), argv[5:])

    def test_a_firewall_that_will_not_load_fails_the_reattach(self) -> None:
        with patch("bot_bottle.backend.macos_container.gateway_attach.container_mod"
                   ".run_container_argv", return_value=self._done("bb-firewall=failed\n")), \
                self.assertRaises(GatewayAttachError):
            MacosGatewayAttacher("net").point_at_gateway(_A, "192.168.128.3")


if __name__ == "__main__":
    unittest.main()
