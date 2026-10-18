"""Unit: stable gateway name via each bottle's /etc/hosts (issue #443).

The gateway's address moves whenever the infra container is recreated. Agents
name it instead of addressing it, and the name resolves through `/etc/hosts` —
a file, so it stays rewritable while the bottle runs, unlike the `environ` the
proxy URL is delivered in. Re-pointing running bottles is part of the reattach
flow (PRD 0081; see test_gateway_attach).
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from bot_bottle.backend.macos_container.gateway_hosts import (
    GATEWAY_HOSTNAME,
    gateway_host_argv,
    set_gateway_host,
)
from bot_bottle.backend.util import ROOT_EXEC_PATH

_MOD = "bot_bottle.backend.macos_container.gateway_hosts"


class TestGatewayHostArgv(unittest.TestCase):
    def _script(self, ip: str = "10.0.0.1") -> str:
        argv = gateway_host_argv(ip)
        self.assertEqual(["/bin/sh", "-c"], argv[:2])
        return argv[2]

    def test_writes_the_address_against_the_stable_name(self) -> None:
        script = self._script("192.168.128.19")
        self.assertIn("192.168.128.19", script)
        self.assertIn(GATEWAY_HOSTNAME, script)

    def test_runs_without_the_agents_path_or_world_writable_tmp(self) -> None:
        """Root must not run anything the agent planted on its PATH, nor follow
        a symlink it planted in /tmp."""
        script = self._script()
        self.assertTrue(script.startswith(f"export PATH={ROOT_EXEC_PATH};"))
        self.assertNotIn("/tmp", script)
        self.assertIn("/root/", script)

    def test_is_idempotent_by_removing_its_own_line_first(self) -> None:
        """Re-pointing must replace the managed entry, not append a second one
        — two entries for the same name would resolve by luck of ordering."""
        self.assertIn("grep -v", self._script())

    def test_preserves_the_rest_of_the_hosts_file_and_its_inode(self) -> None:
        """Filter-and-append, written back with `cat >` rather than `mv`, so
        localhost survives and /etc/hosts keeps its ownership and mode."""
        script = self._script()
        self.assertIn("/etc/hosts >", script)
        self.assertIn("> /etc/hosts", script)
        self.assertNotIn("mv ", script)

    def test_rejects_anything_but_an_address(self) -> None:
        """The address is interpolated into a root shell."""
        with self.assertRaises(ValueError):
            gateway_host_argv("10.0.0.1; rm -rf /")


class TestSetGatewayHost(unittest.TestCase):
    def test_runs_as_root_so_the_agent_cannot_repoint_itself(self) -> None:
        with patch(f"{_MOD}.container_mod.exec_container_as_root") as ex:
            set_gateway_host("bot-bottle-demo", "192.168.128.19")
        self.assertEqual(
            ("bot-bottle-demo", gateway_host_argv("192.168.128.19")), ex.call_args.args)


class TestLaunchWiring(unittest.TestCase):
    def test_launch_sets_the_host_entry_before_reading_the_source_ip(self) -> None:
        """The agent's every URL names the gateway, so the entry has to exist
        before the first connection — which means before the agent execs."""
        import inspect

        from bot_bottle.backend.macos_container import launch

        src = inspect.getsource(launch)
        set_at = src.index("set_gateway_host(plan.container_name")
        exec_at = src.index("wait_container_ipv4_on_network")
        self.assertLess(set_at, exec_at)


if __name__ == "__main__":
    unittest.main()
