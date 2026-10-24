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

_V4 = gw.AGENT_SUBNET
_V6 = gw.AGENT_SUBNET_V6


class TestRules(unittest.TestCase):
    def test_blocks_the_host_and_everything_off_the_subnet(self) -> None:
        rules = fw.render_rules(_V4, _V6)
        self.assertIn(f"block drop in quick inet from {_V4} to (self)\n", rules)
        self.assertIn(f"block drop in quick inet from {_V4} to ! {_V4}\n", rules)
        self.assertIn(f"block drop in quick inet6 from {_V6} to (self)\n", rules)
        self.assertIn(f"block drop in quick inet6 from {_V6} to ! {_V6}\n", rules)

    def test_never_blocks_traffic_within_the_subnet(self) -> None:
        # Agent -> gateway stays on the subnet; no rule may target it.
        for line in fw.render_rules(_V4, _V6).splitlines():
            if line.startswith("block"):
                dest = line.split(" to ", 1)[1]
                self.assertTrue(dest == "(self)" or dest.startswith("! "), line)


class TestDaemon(unittest.TestCase):
    def test_loads_the_anchor_at_boot_and_reapplies_it(self) -> None:
        plist = plistlib.loads(fw.render_daemon())
        self.assertEqual(fw.DAEMON_LABEL, plist["Label"])
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(fw.REAPPLY_SECONDS, plist["StartInterval"])
        script = plist["ProgramArguments"][-1]
        self.assertIn(f"/sbin/pfctl -a {fw.ANCHOR} -f {fw.RULES_PATH}", script)

    def test_enables_pf_only_when_it_is_off(self) -> None:
        # Each `pfctl -E` takes another reference; re-applying every minute
        # must not pile them up.
        script = plistlib.loads(fw.render_daemon())["ProgramArguments"][-1]
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
        self.daemon.write_bytes(fw.render_daemon())
        self.assertEqual("ok", fw.state(_V4, _V6))

    def test_stale_when_the_subnets_changed(self) -> None:
        self.rules.write_text(fw.render_rules("192.168.200.0/24", _V6))
        self.daemon.write_bytes(fw.render_daemon())
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
        fw.uninstall()
        argvs = [c.args[0] for c in self.pfrun.call_args_list]
        self.assertIn(["/sbin/pfctl", "-a", fw.ANCHOR, "-F", "all"], argvs)
        self.assertFalse(self.rules.exists())
        self.assertFalse(self.daemon.exists())

    def test_the_sudo_block_installs_the_same_files(self) -> None:
        block = fw.install_commands(_V4, _V6)
        self.assertIn(fw.render_rules(_V4, _V6), block)
        self.assertIn(fw.render_daemon().decode(), block)
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


if __name__ == "__main__":
    unittest.main()
