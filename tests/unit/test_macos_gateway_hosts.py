"""Unit: stable gateway name via each bottle's /etc/hosts (issue #443).

The gateway's address moves whenever the infra container is recreated. Agents
name it instead of addressing it, and the name resolves through `/etc/hosts` —
a file, so it stays rewritable while the bottle runs, unlike the `environ` the
proxy URL is delivered in.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot_bottle.backend.macos_container.gateway_hosts import (
    GATEWAY_HOSTNAME,
    refresh_gateway_host,
    set_gateway_host,
)

_MOD = "bot_bottle.backend.macos_container.gateway_hosts"


class TestSetGatewayHost(unittest.TestCase):
    def _script(self, exec_root: object) -> str:
        argv = exec_root.call_args.args[1]  # type: ignore[attr-defined]
        self.assertEqual(["sh", "-c"], argv[:2])
        return argv[2]

    def test_writes_the_address_against_the_stable_name(self) -> None:
        with patch(f"{_MOD}.container_mod.exec_container_as_root") as ex:
            set_gateway_host("bot-bottle-demo", "192.168.128.19")
        self.assertEqual("bot-bottle-demo", ex.call_args.args[0])
        script = self._script(ex)
        self.assertIn("192.168.128.19", script)
        self.assertIn(GATEWAY_HOSTNAME, script)

    def test_runs_as_root_so_the_agent_cannot_repoint_itself(self) -> None:
        """The agent runs as `node`. If it could rewrite /etc/hosts it could
        aim its own gateway name elsewhere, so the write must go through the
        root-only helper."""
        with patch(f"{_MOD}.container_mod.exec_container_as_root") as ex:
            set_gateway_host("bot-bottle-demo", "10.0.0.1")
        ex.assert_called_once()

    def test_is_idempotent_by_removing_its_own_line_first(self) -> None:
        """Re-pointing must replace the managed entry, not append a second one
        — two entries for the same name would resolve by luck of ordering."""
        with patch(f"{_MOD}.container_mod.exec_container_as_root") as ex:
            set_gateway_host("bot-bottle-demo", "10.0.0.1")
        self.assertIn("grep -v", self._script(ex))

    def test_preserves_the_rest_of_the_hosts_file(self) -> None:
        """localhost and the container's own name must survive the rewrite."""
        with patch(f"{_MOD}.container_mod.exec_container_as_root") as ex:
            set_gateway_host("bot-bottle-demo", "10.0.0.1")
        script = self._script(ex)
        # Filter-and-append, never a truncating write of just our line.
        self.assertIn("/etc/hosts >", script)
        self.assertIn(">> /tmp/.bb-hosts", script)

    def test_keeps_the_original_inode(self) -> None:
        """`cat >` rather than `mv`: a pre-created /etc/hosts must keep its
        ownership and mode, not be replaced by a root-owned copy."""
        script = None
        with patch(f"{_MOD}.container_mod.exec_container_as_root") as ex:
            set_gateway_host("bot-bottle-demo", "10.0.0.1")
            script = self._script(ex)
        self.assertIn("cat /tmp/.bb-hosts > /etc/hosts", script)
        self.assertNotIn("mv ", script)


class TestRefreshGatewayHost(unittest.TestCase):
    """The re-attach sweep: bottles stranded by an earlier gateway restart get
    re-pointed in place instead of needing a relaunch."""

    def _agents(self, *slugs: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(slug=s) for s in slugs]

    def test_repoints_every_running_bottle(self) -> None:
        with patch(f"{_MOD}.enumerate_active", return_value=self._agents("a", "b")), \
                patch(f"{_MOD}.set_gateway_host") as setter:
            updated = refresh_gateway_host("192.168.128.19")
        self.assertEqual(["bot-bottle-a", "bot-bottle-b"], updated)
        self.assertEqual(
            [("bot-bottle-a", "192.168.128.19"), ("bot-bottle-b", "192.168.128.19")],
            [c.args for c in setter.call_args_list],
        )

    def test_one_failing_bottle_does_not_stop_the_sweep(self) -> None:
        """A container that is already exiting must not block the repair of
        its neighbours, nor fail the launch that triggered the sweep."""
        def _flaky(name: str, _ip: str) -> None:
            if name == "bot-bottle-a":
                raise RuntimeError("container is exiting")

        with patch(f"{_MOD}.enumerate_active", return_value=self._agents("a", "b")), \
                patch(f"{_MOD}.set_gateway_host", side_effect=_flaky), \
                patch(f"{_MOD}.warn") as warn:
            updated = refresh_gateway_host("10.0.0.1")
        self.assertEqual(["bot-bottle-b"], updated)
        warn.assert_called_once()

    def test_no_running_bottles_is_a_clean_no_op(self) -> None:
        with patch(f"{_MOD}.enumerate_active", return_value=[]), \
                patch(f"{_MOD}.set_gateway_host") as setter:
            self.assertEqual([], refresh_gateway_host("10.0.0.1"))
        setter.assert_not_called()



class TestLaunchWiring(unittest.TestCase):
    """Ordering matters: the name must resolve before anything execs, and the
    stranded-bottle sweep must run once the gateway is known to be up."""

    def test_launch_sets_the_host_entry_before_reading_the_source_ip(self) -> None:
        """The agent's every URL names the gateway, so the entry has to exist
        before the first connection — which means before the agent execs."""
        import inspect

        from bot_bottle.backend.macos_container import launch

        src = inspect.getsource(launch)
        set_at = src.index("set_gateway_host(plan.container_name")
        exec_at = src.index("wait_container_ipv4_on_network")
        self.assertLess(set_at, exec_at)

    def test_launch_refreshes_stranded_bottles_after_ensure_gateway(self) -> None:
        import inspect

        from bot_bottle.backend.macos_container import launch

        src = inspect.getsource(launch)
        ensure_at = src.index("endpoint = ensure_gateway()")
        refresh_at = src.index("refresh_gateway_host(endpoint.gateway_ip)")
        self.assertLess(ensure_at, refresh_at)


if __name__ == "__main__":
    unittest.main()
