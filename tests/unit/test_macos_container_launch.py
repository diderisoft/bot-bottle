"""Unit: Apple Container launch argv construction."""

from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from bot_bottle.agent_provider import AgentProvisionPlan
from bot_bottle.backend import BottleSpec
from bot_bottle.backend.macos_container import launch
from bot_bottle.backend.macos_container.bottle_plan import MacosContainerBottlePlan
from bot_bottle.egress import EgressPlan
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.manifest import ManifestIndex

_MANIFEST = ManifestIndex.from_json_obj({
    "bottles": {"dev": {}},
    "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
}).load_for_agent("demo")


def _plan(
    *,
    stage_dir: Path,
    git: bool = False,
    supervise: bool = False,
    agent_git_gate_url: str = "",
    agent_supervise_url: str = "",
    canary: bool = False,
) -> MacosContainerBottlePlan:
    routes_path = stage_dir / "routes.yaml"
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
        canary="fake-canary-value" if canary else "",
        canary_env="CANON_ALPHA_SECRET" if canary else "",
    )
    if git:
        key_path = stage_dir / "origin-key"
        key_path.write_text("key\n", encoding="utf-8")
        known_hosts_path = stage_dir / "origin-known-hosts"
        known_hosts_path.write_text("example.com ssh-ed25519 AAAA\n", encoding="utf-8")
        entrypoint = stage_dir / "git_gate_entrypoint.sh"
        entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
        hook = stage_dir / "git_gate_pre_receive.sh"
        hook.write_text("#!/bin/sh\n", encoding="utf-8")
        access_hook = stage_dir / "git_gate_access_hook.sh"
        access_hook.write_text("#!/bin/sh\n", encoding="utf-8")
        upstream = SimpleNamespace(
            name="origin",
            identity_file=str(key_path),
            known_hosts_file=known_hosts_path,
        )
        git_gate_plan = SimpleNamespace(
            upstreams=(upstream,),
            entrypoint_script=entrypoint,
            hook_script=hook,
            access_hook_script=access_hook,
        )
    else:
        git_gate_plan = SimpleNamespace(upstreams=())
    supervise_plan = (
        SimpleNamespace(
            queue_dir=Path("/state/supervise/queue"),
            db_path=Path("/state/bot-bottle.db"),
        )
        if supervise else None
    )
    agent_provision = SimpleNamespace(
        guest_env={"LITERAL": "value"},
        provisioned_env={"CODEX_HOME": "/run/codex-home"},
    )
    return cast(MacosContainerBottlePlan, SimpleNamespace(
        spec=SimpleNamespace(),
        manifest=_MANIFEST,
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
        self.assertIn(
            f"type=bind,source={self.stage_dir},target=/etc/egress,readonly",
            argv,
        )
        self.assertIn(
            "type=bind,source=/state/bot-bottle.db,target=/run/supervise/bot-bottle.db",
            argv,
        )
        self.assertIn(
            "type=bind,source=/state/supervise/queue,target=/run/supervise/queue",
            argv,
        )

    def test_sidecar_argv_registers_canary_env_as_sensitive(self):
        plan = _plan(stage_dir=self.stage_dir, canary=True)
        argv = launch._sidecar_run_argv(
            plan,
            "bot-bottle-sidecars-dev-abc",
            "bot-bottle-net-dev-abc",
            "bot-bottle-egress-dev-abc",
        )
        self.assertIn("CANON_ALPHA_SECRET=fake-canary-value", argv)
        self.assertIn("BOT_BOTTLE_SENSITIVE_PREFIXES=CANON_ALPHA_SECRET", argv)

    def test_agent_argv_receives_canary_env(self):
        plan = _plan(stage_dir=self.stage_dir, canary=True)
        argv = launch._agent_run_argv(
            plan,
            "bot-bottle-net-dev-abc",
            "192.0.2.10",
        )
        self.assertIn("CANON_ALPHA_SECRET=fake-canary-value", argv)

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

    def test_git_gate_daemons_are_ready_gated(self):
        plan = _plan(stage_dir=self.stage_dir, git=True)
        self.assertEqual(
            ("egress", "git-gate", "git-http"),
            launch._sidecar_daemons(plan),
        )
        self.assertIn(
            "BOT_BOTTLE_GIT_GATE_READY_FILE=/run/git-gate/ready",
            launch._sidecar_env_entries(plan),
        )

    def test_stamp_agent_urls_includes_git_http_when_git_gate_exists(self):
        plan = _plan(stage_dir=self.stage_dir, git=True, supervise=True)
        with patch.object(launch.dataclasses, "replace") as replace:
            launch._stamp_agent_urls(plan, "192.168.128.2")
        replace.assert_called_once_with(
            plan,
            agent_proxy_url="http://192.168.128.2:9099",
            agent_git_gate_url="http://192.168.128.2:9420",
            agent_supervise_url="http://192.168.128.2:9100/",
        )

    def test_macos_plan_uses_http_git_gate_rewrites(self):
        base = _plan(
            stage_dir=self.stage_dir,
            git=True,
            agent_git_gate_url="http://192.168.128.2:9420",
        )
        plan = MacosContainerBottlePlan(
            spec=base.spec,
            manifest=base.manifest,
            stage_dir=base.stage_dir,
            git_gate_plan=base.git_gate_plan,
            egress_plan=base.egress_plan,
            supervise_plan=base.supervise_plan,
            agent_provision=base.agent_provision,
            slug=base.slug,
            forwarded_env=base.forwarded_env,
            agent_git_gate_url=base.agent_git_gate_url,
        )
        self.assertEqual(
            "192.168.128.2:9420",
            plan.git_gate_insteadof_host,
        )
        self.assertEqual("http", plan.git_gate_insteadof_scheme)

    def test_stage_git_gate_copies_files_and_releases_ready_marker(self):
        plan = _plan(stage_dir=self.stage_dir, git=True)
        with (
            patch.object(launch.container_mod, "exec_container") as exec_container,
            patch.object(launch.container_mod, "copy_into_container") as copy_in,
        ):
            launch._stage_git_gate(plan, "sidecar")

        exec_container.assert_any_call(
            "sidecar",
            [
                "mkdir",
                "-p",
                "/etc/git-gate",
                "/git-gate/creds",
                "/git",
                "/run/git-gate",
            ],
        )
        copied = [call.args for call in copy_in.call_args_list]
        self.assertIn(
            (
                "sidecar",
                str(self.stage_dir / "git_gate_entrypoint.sh"),
                "/git-gate-entrypoint.sh",
            ),
            copied,
        )
        self.assertIn(
            (
                "sidecar",
                str(self.stage_dir / "origin-key"),
                "/git-gate/creds/origin-key",
            ),
            copied,
        )
        self.assertIn(
            (
                "sidecar",
                str(self.stage_dir / "origin-known-hosts"),
                "/git-gate/creds/origin-known_hosts",
            ),
            copied,
        )
        self.assertIn(
            "touch /run/git-gate/ready",
            exec_container.call_args_list[-1].args[1][-1],
        )


def _build_plan(stage_dir: Path) -> MacosContainerBottlePlan:
    return MacosContainerBottlePlan(
        spec=cast(BottleSpec, SimpleNamespace()),
        manifest=_MANIFEST,
        stage_dir=stage_dir,
        git_gate_plan=cast(GitGatePlan, SimpleNamespace(upstreams=())),
        egress_plan=cast(EgressPlan, SimpleNamespace(canary="")),
        supervise_plan=None,
        agent_provision=AgentProvisionPlan(
            template="claude",
            command="claude",
            prompt_mode="append_file",
            image="bot-bottle-agent:latest",
            dockerfile="/repo/Dockerfile",
            guest_home="/home/node",
            instance_name="bot-bottle-dev-abc",
            prompt_file=stage_dir / "prompt.txt",
            guest_env={},
        ),
        slug="dev-abc",
        forwarded_env={},
    )


class TestMacosContainerLaunchCommittedImage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.stage_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_build_images_uses_committed_image_when_present(self):
        plan = _build_plan(self.stage_dir)
        calls = []

        def fake_build(image: str, context: str, *, dockerfile: str = "") -> None:
            calls.append((image, context, dockerfile))

        with patch.object(
            launch, "read_committed_image",
            return_value="bot-bottle-committed-dev-abc:latest",
        ), patch.object(
            launch.container_mod, "image_exists", return_value=True,
        ), patch.object(
            launch.container_mod, "build_image", side_effect=fake_build,
        ), patch.object(launch, "info"):
            updated = launch._build_images(plan)

        self.assertEqual("bot-bottle-committed-dev-abc:latest", updated.image)
        self.assertEqual(1, len(calls))
        self.assertEqual(launch.SIDECAR_BUNDLE_IMAGE, calls[0][0])

    def test_build_images_builds_agent_when_committed_image_missing(self):
        plan = _build_plan(self.stage_dir)
        calls = []

        def fake_build(image: str, context: str, *, dockerfile: str = "") -> None:
            calls.append((image, context, dockerfile))

        with patch.object(
            launch, "read_committed_image",
            return_value="bot-bottle-committed-dev-abc:latest",
        ), patch.object(
            launch.container_mod, "image_exists", return_value=False,
        ), patch.object(
            launch.container_mod, "build_image", side_effect=fake_build,
        ):
            updated = launch._build_images(plan)

        self.assertEqual("bot-bottle-agent:latest", updated.image)
        self.assertEqual(2, len(calls))
        self.assertEqual("bot-bottle-agent:latest", calls[1][0])


if __name__ == "__main__":
    unittest.main()
