"""Unit: Apple Container launch argv construction."""

from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from bot_bottle.backend.macos_container import launch
from bot_bottle.backend.macos_container.bottle_plan import MacosContainerBottlePlan


def _plan(
    *,
    stage_dir: Path,
    git: bool = False,
    supervise: bool = False,
    agent_git_gate_url: str = "",
    agent_supervise_url: str = "",
) -> MacosContainerBottlePlan:
    routes_path = stage_dir / "source-routes.yaml"
    routes_path.write_text("routes: []\n", encoding="utf-8")
    ca_dir = stage_dir / "egress-ca"
    ca_dir.mkdir(exist_ok=True)
    ca_path = ca_dir / "mitmproxy-ca.pem"
    ca_path.write_text("ca\n", encoding="utf-8")
    egress_plan = SimpleNamespace(
        mitmproxy_ca_host_path=ca_path,
        routes_path=routes_path,
        routes=("route",),
        token_env_map={"EGRESS_TOKEN_0": "HOST_TOKEN"},
    )
    if git:
        upstream = SimpleNamespace(
            name="origin",
            identity_file="/host/key",
            known_hosts_file=Path("/host/known_hosts"),
        )
        git_gate_plan = SimpleNamespace(
            upstreams=(upstream,),
            entrypoint_script=Path("/state/git/entrypoint.sh"),
            hook_script=Path("/state/git/pre-receive"),
            access_hook_script=Path("/state/git/access.sh"),
        )
    else:
        git_gate_plan = SimpleNamespace(upstreams=())
    supervise_plan = (
        SimpleNamespace(queue_dir=Path("/state/supervise/queue"))
        if supervise else None
    )
    agent_provision = SimpleNamespace(
        guest_env={"LITERAL": "value"},
        provisioned_env={"CODEX_HOME": "/run/codex-home"},
    )
    return cast(MacosContainerBottlePlan, SimpleNamespace(
        stage_dir=stage_dir,
        slug="dev-abc",
        container_name="bot-bottle-dev-abc",
        image="bot-bottle-agent:latest",
        forwarded_env={"OAUTH_TOKEN": "host-value"},
        egress_plan=egress_plan,
        git_gate_plan=git_gate_plan,
        supervise_plan=supervise_plan,
        agent_provision=agent_provision,
        agent_git_gate_url=agent_git_gate_url,
        agent_supervise_url=agent_supervise_url,
    ))


class TestMacosContainerLaunchArgv(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.stage_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_sidecar_argv_uses_egress_network_first_and_explicit_dns(self):
        plan = _plan(stage_dir=self.stage_dir, supervise=True)
        with patch.object(launch.os, "environ", {
            "BOT_BOTTLE_MACOS_CONTAINER_DNS": "9.9.9.9",
        }):
            argv = launch._sidecar_run_argv(
                plan,
                "bot-bottle-sidecars-dev-abc",
                "bot-bottle-net-dev-abc",
                "bot-bottle-egress-dev-abc",
            )
        self.assertEqual(
            [
                "--network", "bot-bottle-egress-dev-abc",
                "--network", "bot-bottle-net-dev-abc",
            ],
            argv[argv.index("--network"):argv.index("--dns")],
        )
        self.assertIn("--dns", argv)
        self.assertEqual("9.9.9.9", argv[argv.index("--dns") + 1])
        self.assertIn(
            "BOT_BOTTLE_SIDECAR_DAEMONS=egress,supervise",
            argv,
        )
        self.assertIn("EGRESS_TOKEN_0", argv)
        self.assertIn(
            f"type=bind,source={self.stage_dir / 'egress-ca'},target=/home/mitmproxy/.mitmproxy",
            argv,
        )
        routes_dir = self.stage_dir / "macos-container-egress"
        self.assertIn(
            f"type=bind,source={routes_dir},target=/etc/egress,readonly",
            argv,
        )
        self.assertEqual(
            "routes: []\n",
            (routes_dir / "routes.yaml").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "type=bind,source=/state/supervise/queue,target=/run/supervise/queue",
            argv,
        )

    def test_agent_env_points_proxy_at_sidecar_ip(self):
        plan = _plan(
            stage_dir=self.stage_dir,
            agent_git_gate_url="http://192.168.128.2:9420",
            agent_supervise_url="http://192.168.128.2:9100/",
        )
        env = launch._agent_env_entries(plan, "192.168.128.2")
        self.assertIn("HTTPS_PROXY=http://192.168.128.2:9099", env)
        self.assertIn("HTTP_PROXY=http://192.168.128.2:9099", env)
        self.assertIn("https_proxy=http://192.168.128.2:9099", env)
        self.assertIn("http_proxy=http://192.168.128.2:9099", env)
        self.assertIn("NO_PROXY=localhost,127.0.0.1,192.168.128.2", env)
        self.assertIn("NODE_EXTRA_CA_CERTS=/usr/local/share/ca-certificates/bot-bottle-mitm-ca.crt", env)
        self.assertIn("SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt", env)
        self.assertIn("REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt", env)
        self.assertIn("GIT_GATE_URL=http://192.168.128.2:9420", env)
        self.assertIn("MCP_SUPERVISE_URL=http://192.168.128.2:9100/", env)
        self.assertIn("LITERAL=value", env)
        self.assertIn("OAUTH_TOKEN", env)
        self.assertNotIn("CODEX_HOME", env)

    def test_agent_run_uses_internal_network_only(self):
        plan = _plan(stage_dir=self.stage_dir)
        argv = launch._agent_run_argv(
            plan, "bot-bottle-net-dev-abc", "192.168.128.2",
        )
        self.assertIn("--network", argv)
        self.assertEqual("bot-bottle-net-dev-abc", argv[argv.index("--network") + 1])
        self.assertNotIn("bot-bottle-egress-dev-abc", argv)
        self.assertEqual(["bot-bottle-agent:latest", "sleep", "2147483647"], argv[-3:])

    def test_git_gate_is_blocked_until_safe_key_delivery_exists(self):
        plan = _plan(stage_dir=self.stage_dir, git=True)
        with patch.object(launch, "die", side_effect=SystemExit("die")):
            with self.assertRaises(SystemExit):
                launch._validate_supported_plan(plan)


if __name__ == "__main__":
    unittest.main()
