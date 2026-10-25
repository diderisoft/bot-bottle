"""Unit: the macOS host firewall (pf anchor + LaunchDaemon) and the pinned
agent subnets it blocks."""

from __future__ import annotations

import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.backend.macos_container import gateway as gw
from bot_bottle.backend.macos_container import host_firewall as fw
from bot_bottle.backend.macos_container import util as mac_util
from bot_bottle.lib.log import Die

_V4 = "192.168.128.0/24"
_V6 = gw.AGENT_SUBNET_V6


class TestRules(unittest.TestCase):
    def test_blocks_the_host_and_everything_off_the_subnet(self) -> None:
        rules = fw.render_rules(_V4, _V6)
        self.assertIn(f"block drop in quick inet from {_V4} to 192.168.128.1\n", rules)
        self.assertIn(f"block drop in quick inet from {_V4} to (self)\n", rules)
        self.assertIn(f"block drop in quick inet from {_V4} to ! {_V4}\n", rules)
        self.assertIn(f"block drop in quick inet6 from {_V6} to (self)\n", rules)
        self.assertIn(f"block drop in quick inet6 from {_V6} to ! {_V6}\n", rules)

    def test_blocks_nothing_else_on_the_subnet(self) -> None:
        # Agent -> gateway stays on the subnet; only the host's own address
        # on it may be targeted.
        hosts = {"192.168.128.1"}
        for line in fw.render_rules(_V4, _V6).splitlines():
            if line.startswith("block"):
                dest = line.split(" to ", 1)[1]
                self.assertTrue(dest in hosts or dest == "(self)" or dest.startswith("! "), line)


class TestDaemon(unittest.TestCase):
    def test_loads_the_anchor_at_boot_and_reapplies_it(self) -> None:
        plist = plistlib.loads(fw.render_daemon(_V4))
        self.assertEqual(fw.DAEMON_LABEL, plist["Label"])
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(fw.REAPPLY_SECONDS, plist["StartInterval"])
        script = plist["ProgramArguments"][-1]
        self.assertIn(f"/sbin/pfctl -a {fw.ANCHOR} -f {fw.RULES_PATH}", script)

    def test_enables_pf_only_when_it_is_off(self) -> None:
        # Each `pfctl -E` takes another reference; re-applying every minute
        # must not pile them up.
        script = plistlib.loads(fw.render_daemon(_V4))["ProgramArguments"][-1]
        self.assertIn("|| /sbin/pfctl -E", script)

    def test_hooks_into_the_existing_com_apple_anchor(self) -> None:
        self.assertTrue(fw.ANCHOR.startswith("com.apple/"))


class TestState(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.rules = Path(tmp.name) / "rules"
        self.daemon = Path(tmp.name) / "daemon.plist"
        self.enterContext(patch.object(fw, "RULES_PATH", self.rules))
        self.enterContext(patch.object(fw, "DAEMON_PATH", self.daemon))

    def test_missing(self) -> None:
        self.assertEqual("missing", fw.state(_V4, _V6))

    def test_ok_when_both_files_match(self) -> None:
        self.rules.write_text(fw.render_rules(_V4, _V6))
        self.daemon.write_bytes(fw.render_daemon(_V4))
        self.assertEqual("ok", fw.state(_V4, _V6))

    def test_stale_when_the_subnets_changed(self) -> None:
        self.rules.write_text(fw.render_rules("192.168.200.0/24", _V6))
        self.daemon.write_bytes(fw.render_daemon(_V4))
        self.assertEqual("stale", fw.state(_V4, _V6))


class TestInstall(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.rules = Path(tmp.name) / "anchors" / "io.bot-bottle"
        self.daemon = Path(tmp.name) / "io.bot-bottle.pf.plist"
        self.enterContext(patch.object(fw, "RULES_PATH", self.rules))
        self.enterContext(patch.object(fw, "DAEMON_PATH", self.daemon))
        self.pfrun = self.enterContext(patch.object(
            fw.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0, "", "")))

    def test_writes_both_files_and_reloads_the_daemon(self) -> None:
        fw.install(_V4, _V6)
        self.assertEqual("ok", fw.state(_V4, _V6))
        argvs = [c.args[0] for c in self.pfrun.call_args_list]
        self.assertEqual(["launchctl", "bootout", f"system/{fw.DAEMON_LABEL}"], argvs[0])
        self.assertEqual(["launchctl", "bootstrap", "system", str(self.daemon)], argvs[1])

    def test_uninstall_unloads_flushes_and_removes(self) -> None:
        fw.install(_V4, _V6)
        self.pfrun.reset_mock()
        with patch.object(fw, "agent_bridge", return_value="bridge103"):
            fw.uninstall(_V4)
        argvs = [c.args[0] for c in self.pfrun.call_args_list]
        self.assertIn(["/sbin/pfctl", "-a", fw.ANCHOR, "-F", "all"], argvs)
        self.assertIn(["/sbin/ifconfig", "bridge103", "inet6", "-ifdisabled"], argvs)
        self.assertFalse(self.rules.exists())
        self.assertFalse(self.daemon.exists())

    def test_the_sudo_block_installs_the_same_files(self) -> None:
        block = fw.install_commands(_V4, _V6)
        self.assertIn(fw.render_rules(_V4, _V6), block)
        self.assertIn(fw.render_daemon(_V4).decode(), block)
        self.assertIn(f"sudo launchctl bootstrap system {fw.DAEMON_PATH}", block)


def _inspect(v4: str, v6: str) -> subprocess.CompletedProcess[str]:
    # `container network inspect` escapes "/" in JSON.
    body = json.dumps([{"status": {"ipv4Subnet": v4, "ipv6Subnet": v6}}]).replace("/", "\\/")
    return subprocess.CompletedProcess([], 0, body, "")


class TestPinnedAgentNetwork(unittest.TestCase):
    def test_network_subnets_normalizes(self) -> None:
        with patch.object(mac_util, "run_container_argv",
                          return_value=_inspect("192.168.128.0/24", "FD62:6F74:6274:6C65::/64")):
            self.assertEqual((_V4, _V6), mac_util.network_subnets("n"))

    def test_network_subnets_takes_the_ipv6_prefix_as_status_reports_it(self) -> None:
        # What a real network's status says: the host's address on the prefix.
        with patch.object(mac_util, "run_container_argv",
                          return_value=_inspect("192.168.128.0/24", "fd62:6f74:6274:6c65::1/64")):
            self.assertEqual((_V4, _V6), mac_util.network_subnets("n"))

    def setUp(self) -> None:
        self.enterContext(patch.object(gw.host_firewall, "agent_subnet", return_value=_V4))
        self.conflicts = self.enterContext(
            patch.object(gw.host_firewall, "conflicts", return_value=[]))

    def test_a_subnet_this_mac_uses_is_refused_before_creating_anything(self) -> None:
        self.conflicts.return_value = ["en0 has 192.168.128.7/24"]
        with patch.object(gw.container_mod, "create_network") as create, \
                self.assertRaises(Die) as cm:
            gw.ensure_networks()
        create.assert_not_called()
        self.assertIn("en0 has 192.168.128.7/24", cm.exception.message)
        self.assertIn("backend setup --backend=macos-container", cm.exception.message)

    def test_creates_the_agent_network_with_the_pinned_subnets(self) -> None:
        with patch.object(gw.container_mod, "create_network") as create, \
             patch.object(gw.container_mod, "network_subnets", return_value=(_V4, _V6)):
            gw.ensure_networks()
        create.assert_any_call(gw.GATEWAY_NETWORK, internal=True,
                               subnet=_V4, subnet_v6=_V6)

    def test_an_existing_network_with_other_subnets_is_refused(self) -> None:
        with patch.object(gw.container_mod, "create_network"), \
             patch.object(gw.container_mod, "network_subnets",
                          return_value=(_V4, "fdb1:ed40:412b:7db5::/64")), \
             self.assertRaises(Die) as cm:
            gw.ensure_networks()
        self.assertIn(f"container network rm {gw.GATEWAY_NETWORK}", cm.exception.message)

    def test_create_network_passes_the_subnets(self) -> None:
        with patch.object(mac_util.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            mac_util.create_network("n", internal=True, subnet=_V4, subnet_v6=_V6)
        argv = run.call_args.args[0]
        self.assertEqual(["--internal", "--subnet", _V4, "--subnet-v6", _V6, "n"], argv[-6:])


_IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
	inet 127.0.0.1 netmask 0xff000000
bridge103: flags=8a63<UP,BROADCAST,SMART,RUNNING,ALLMULTI,SIMPLEX,MULTICAST> mtu 1500
	inet 192.168.128.1 netmask 0xffffff00 broadcast 192.168.128.255
	inet6 fe80::bcd0:74ff:fed1:b767%bridge103 prefixlen 64 scopeid 0x24
	nd6 options=201<PERFORMNUD,DAD>
bridge104: flags=8a63<UP> mtu 1500
	inet 192.168.129.1 netmask 0xffffff00 broadcast 192.168.129.255
"""


def _ifconfig(out: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, out, "")


class TestAgentBridgeIPv6(unittest.TestCase):
    def test_finds_the_bridge_holding_the_hosts_address(self) -> None:
        with patch.object(fw.subprocess, "run", return_value=_ifconfig(_IFCONFIG)):
            self.assertEqual("bridge103", fw.agent_bridge(_V4))
        with patch.object(fw.subprocess, "run", return_value=_ifconfig("lo0: flags=1\n")):
            self.assertIsNone(fw.agent_bridge(_V4))

    def test_reads_whether_ipv6_is_off(self) -> None:
        on = "\tnd6 options=201<PERFORMNUD,DAD>\n"
        off = "\tnd6 options=209<PERFORMNUD,IFDISABLED,DAD>\n"
        with patch.object(fw.subprocess, "run", return_value=_ifconfig(on)):
            self.assertFalse(fw.ipv6_off("bridge103"))
        with patch.object(fw.subprocess, "run", return_value=_ifconfig(off)):
            self.assertTrue(fw.ipv6_off("bridge103"))

    def test_waits_for_the_daemon_then_gives_up(self) -> None:
        now = [0.0]
        def sleep(s: float) -> None:
            now[0] += s
        with patch.object(fw, "agent_bridge", return_value="bridge103"), \
                patch.object(fw, "ipv6_off", side_effect=[False, False, True]):
            self.assertTrue(fw.wait_ipv6_off(_V4, sleep=sleep, clock=lambda: now[0]))
        with patch.object(fw, "agent_bridge", return_value="bridge103"), \
                patch.object(fw, "ipv6_off", return_value=False):
            self.assertFalse(fw.wait_ipv6_off(_V4, timeout=10, sleep=sleep,
                                              clock=lambda: now[0]))

    def test_the_daemon_turns_ipv6_off_on_the_agent_bridge(self) -> None:
        script = plistlib.loads(fw.render_daemon(_V4))["ProgramArguments"][-1]
        self.assertIn(r"/inet 192\.168\.128\.1 /", script)
        self.assertIn('/sbin/ifconfig "$br" inet6 ifdisabled', script)
        # The lookup the daemon runs finds the right bridge in real ifconfig output.
        lookup = script.split("br=$(", 1)[1].split("); [ -z", 1)[0].replace("/sbin/ifconfig | ", "")
        done = subprocess.run(["/bin/sh", "-c", lookup], input=_IFCONFIG, capture_output=True,
                              text=True, check=True)
        self.assertEqual("bridge103", done.stdout.strip())


_LAN_IFCONFIG = """en0: flags=8863<UP> mtu 1500
\tinet 192.168.128.1 netmask 0xffffff00 broadcast 192.168.128.255
bridge103: flags=8a63<UP> mtu 1500
\tinet 192.168.200.1 netmask 0xffffff00 broadcast 192.168.200.255
"""
_NETSTAT = """Routing tables

Internet:
Destination        Gateway            Flags               Netif Expire
default            192.168.1.1        UGScg                 en8
100.64/10          link#27            UCS                 utun7
127                127.0.0.1          UCS                   lo0
172.30             link#40            UCS                 utun4
192.168.200        link#36            UC              bridge103      !
"""


def _commands(ifconfig: str, netstat: str = "Internet:\n"):  # type: ignore[no-untyped-def]
    def run(argv: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
        out = ifconfig if argv[0].endswith("ifconfig") else netstat
        return subprocess.CompletedProcess(argv, 0, out, "")
    return run


class TestSubnetChoice(unittest.TestCase):
    def test_netstat_destinations_read_as_networks(self) -> None:
        self.assertEqual("192.168.1.0/24", str(fw._route_network("192.168.1")))  # pylint: disable=protected-access
        self.assertEqual("127.0.0.0/8", str(fw._route_network("127")))  # pylint: disable=protected-access
        self.assertEqual("100.64.0.0/10", str(fw._route_network("100.64/10")))  # pylint: disable=protected-access
        self.assertEqual("10.0.0.5/32", str(fw._route_network("10.0.0.5")))  # pylint: disable=protected-access
        self.assertIsNone(fw._route_network("default"))  # pylint: disable=protected-access
        self.assertIsNone(fw._route_network("fe80::%utun0"))  # pylint: disable=protected-access

    def test_finds_lan_addresses_and_vpn_routes_but_not_our_bridge(self) -> None:
        with patch.object(fw.subprocess, "run", side_effect=_commands(_LAN_IFCONFIG, _NETSTAT)):
            self.assertEqual(["en0 has 192.168.128.1/24"], fw.conflicts("192.168.128.0/24"))
            self.assertEqual([], fw.conflicts("192.168.200.0/24"))
            self.assertEqual(["utun4 routes 172.30.0.0/16"], fw.conflicts("172.30.128.0/24"))
            self.assertEqual(["utun7 routes 100.64.0.0/10"], fw.conflicts("100.100.0.0/24"))

    def test_a_lan_on_the_hosts_agent_address_is_never_taken_for_the_bridge(self) -> None:
        with patch.object(fw.subprocess, "run", side_effect=_commands(_LAN_IFCONFIG)):
            self.assertIsNone(fw.agent_bridge("192.168.128.0/24"))
        script = plistlib.loads(fw.render_daemon(_V4))["ProgramArguments"][-1]
        lookup = script.split("br=$(", 1)[1].split("); [ -z", 1)[0].replace("/sbin/ifconfig | ", "")
        done = subprocess.run(["/bin/sh", "-c", lookup], input=_LAN_IFCONFIG, capture_output=True,
                              text=True, check=True)
        self.assertEqual("", done.stdout.strip())  # en0 holds .1, and isn't touched

    def test_reads_the_installed_subnet_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules"
            rules.write_text(fw.render_rules("10.213.128.0/24", _V6))
            with patch.object(fw, "RULES_PATH", rules):
                self.assertEqual("10.213.128.0/24", fw.installed_subnet())
                self.assertEqual("10.213.128.0/24", fw.agent_subnet())
            with patch.object(fw, "RULES_PATH", Path(tmp) / "missing"):
                self.assertIsNone(fw.installed_subnet())
                self.assertEqual(fw.CANDIDATE_SUBNETS[0], fw.agent_subnet())

    def test_keeps_a_free_installed_subnet_else_takes_the_next_free_one(self) -> None:
        taken = {"192.168.128.0/24": ["en0 has 192.168.128.1/24"], "192.168.200.0/24": ["x"]}

        def conflicts(subnet: str) -> list[str]:
            return taken.get(subnet, [])
        with patch.object(fw, "conflicts", side_effect=conflicts):
            with patch.object(fw, "installed_subnet", return_value="10.213.128.0/24"):
                self.assertEqual(("10.213.128.0/24", []), fw.choose_subnet())
            with patch.object(fw, "installed_subnet", return_value="192.168.128.0/24"):
                self.assertEqual(("10.213.128.0/24", ["en0 has 192.168.128.1/24"]),
                                 fw.choose_subnet())
        with patch.object(fw, "conflicts", return_value=["x"]), \
                patch.object(fw, "installed_subnet", return_value=None), \
                self.assertRaises(RuntimeError):
            fw.choose_subnet()


if __name__ == "__main__":
    unittest.main()
