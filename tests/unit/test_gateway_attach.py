"""PRD 0081: reattaching running bottles to a freshly (re)started gateway."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from bot_bottle.contract.hostctl import Settings
from bot_bottle.backend import gateway_attach as ga
from bot_bottle.backend.gateway_attach import (
    GatewayAttachError, GatewayAttacher, RunningBottle,
)
from bot_bottle.backend.util import ROOT_EXEC_PATH
from bot_bottle.control.gateway import GatewayError
from bot_bottle.control.client import OrchestratorClientError

_A = RunningBottle(ref="ref-a", slug="a", source_ip="10.0.0.2")
_B = RunningBottle(ref="ref-b", slug="b", source_ip="10.0.0.3")


class _Attacher(GatewayAttacher):
    def __init__(self, bottles: list[RunningBottle], *, fail_ca: set[str] | None = None,
                 secrets: dict[str, str] | None = None) -> None:
        self.bottles = bottles
        self.fail_ca = fail_ca or set()
        self.secrets = secrets if secrets is not None else {b.slug: f"s-{b.slug}" for b in bottles}
        self.installed: list[tuple[str, str]] = []
        self.pointed: list[tuple[str, str]] = []

    def running_bottles(self) -> list[RunningBottle]:
        return self.bottles

    def point_at_gateway(self, bottle: RunningBottle, address: str) -> None:
        self.pointed.append((bottle.slug, address))

    def install_ca(self, bottle: RunningBottle, ca_pem: str) -> None:
        if bottle.slug in self.fail_ca:
            raise GatewayAttachError("ssh: connection refused")
        self.installed.append((bottle.slug, ca_pem))

    def read_env_var_secret(self, bottle: RunningBottle) -> str:
        return self.secrets.get(bottle.slug, "")


class ReattachRunningBottles(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = MagicMock()
        self.gateway.ca_cert_pem.return_value = "PEM"
        self.gateway.address.return_value = "10.0.0.9"
        client = patch.object(ga, "OrchestratorClient")
        self.client = client.start().return_value
        self.addCleanup(client.stop)
        self.client.list_bottles.return_value = [
            {"bottle_id": "id-a", "source_ip": "10.0.0.2"},
            {"bottle_id": "id-b", "source_ip": "10.0.0.3"},
        ]
        git_gate = patch.object(ga, "reprovision_git_gate")
        self.git_gate = git_gate.start()
        self.addCleanup(git_gate.stop)

    def _run(self, attacher: GatewayAttacher) -> None:
        ga.reattach_running_bottles(
            attacher, orchestrator_url="http://orch", gateway=self.gateway)

    def test_restores_address_ca_git_gate_and_egress_tokens_for_every_bottle(self) -> None:
        attacher = _Attacher([_A, _B])
        self._run(attacher)
        self.assertEqual([("a", "10.0.0.9"), ("b", "10.0.0.9")], attacher.pointed)
        self.assertEqual([("a", "PEM"), ("b", "PEM")], attacher.installed)
        transport = self.gateway.provisioning_transport.return_value
        self.git_gate.assert_any_call(transport, "id-a", "a")
        self.git_gate.assert_any_call(transport, "id-b", "b")
        self.client.reprovision_gateway.assert_any_call("id-a", "s-a")
        self.client.reprovision_gateway.assert_any_call("id-b", "s-b")

    def test_no_running_bottles_touches_nothing(self) -> None:
        self._run(_Attacher([]))
        self.gateway.ca_cert_pem.assert_not_called()
        self.client.list_bottles.assert_not_called()

    def test_every_bottle_is_attempted_and_failures_raised_together(self) -> None:
        attacher = _Attacher([_A, _B], fail_ca={"a"})
        with self.assertRaises(GatewayAttachError) as raised:
            self._run(attacher)
        self.assertIn("a: ssh: connection refused", str(raised.exception))
        self.assertEqual([("b", "PEM")], attacher.installed)  # b still reattached
        self.client.reprovision_gateway.assert_called_once_with("id-b", "s-b")

    def test_token_restore_failure_is_reported_against_its_bottle(self) -> None:
        self.client.reprovision_gateway.side_effect = OrchestratorClientError("HTTP 400")
        with self.assertRaises(GatewayAttachError) as raised:
            self._run(_Attacher([_A]))
        self.assertIn("a: HTTP 400", str(raised.exception))

    def test_missing_secret_is_a_failure_not_a_silent_skip(self) -> None:
        with self.assertRaises(GatewayAttachError) as raised:
            self._run(_Attacher([_A], secrets={}))
        self.assertIn("a: its ENV_VAR_SECRET is missing", str(raised.exception))
        self.client.reprovision_gateway.assert_not_called()

    def test_unregistered_or_addressless_bottles_are_skipped(self) -> None:
        # Mid-launch (no address yet, or not registered until the pair is up)
        # or mid-teardown: nothing to restore, and not a failure.
        self.client.list_bottles.return_value = [{"bottle_id": "id-a", "source_ip": "10.0.0.2"}]
        pending = RunningBottle(ref="ref-c", slug="c", source_ip="")
        attacher = _Attacher([_A, _B, pending])
        with patch.object(ga, "info") as info:
            self._run(attacher)
        self.assertEqual([("a", "PEM")], attacher.installed)
        self.assertTrue(any("skipped 2" in c.args[0] for c in info.call_args_list))

    def test_unreachable_gateway_fails_before_any_bottle(self) -> None:
        self.gateway.ca_cert_pem.side_effect = GatewayError("no CA yet")
        attacher = _Attacher([_A])
        with self.assertRaises(GatewayAttachError):
            self._run(attacher)
        self.assertEqual([], attacher.installed)


class ReprovisionGitGate(unittest.TestCase):
    transport = MagicMock()

    def test_uses_the_snapshot_and_renders_current_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "upstreams.json").write_text(json.dumps([
                {"name": "repo", "upstream_url": "ssh://git@forge/o/r.git",
                 "upstream_host": "forge", "upstream_port": "22",
                 "identity_file": str(state / "repo-key"), "known_host_key": "ssh-ed25519 AAA"},
            ]))
            (state / "git_gate_pre_receive.sh").write_text("stale hook")
            with patch.object(ga, "git_gate_state_dir", return_value=state), \
                 patch.object(ga, "provision_git_gate") as provision:
                ga.reprovision_git_gate(self.transport, "id-a", "a")
            hook_text = (state / "git_gate_pre_receive.sh").read_text()
        transport, bottle_id, plan = provision.call_args.args
        self.assertEqual((self.transport, "id-a"), (transport, bottle_id))
        self.assertEqual(str(state / "repo-key"), plan.upstreams[0].identity_file)
        self.assertNotEqual("stale hook", hook_text)  # rendered from current code

    def test_no_snapshot_means_no_git_upstreams(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(ga, "git_gate_state_dir", return_value=Path(tmp)), \
             patch.object(ga, "provision_git_gate") as provision:
            ga.reprovision_git_gate(self.transport, "id-a", "a")
        provision.assert_not_called()


class InfraServiceTrigger(unittest.TestCase):
    """The bring-up harness decides when to reattach, for every backend."""

    def setUp(self) -> None:
        from bot_bottle.backend.docker.infra import DockerInfraService
        self.svc = DockerInfraService(Settings(), port=8099)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = patch("bot_bottle.backend.infra_service.bot_bottle_root",
                     return_value=Path(tmp.name))
        root.start()
        self.addCleanup(root.stop)
        reattach = patch("bot_bottle.backend.infra_service.reattach_running_bottles")
        self.reattach = reattach.start()
        self.addCleanup(reattach.stop)
        # A release matches: building locally is opt-in (test_infra_build_opt_in).
        matches = patch("bot_bottle.backend.infra_service.published_infra_matches",
                        return_value=True)
        matches.start()
        self.addCleanup(matches.stop)
        gateway = patch.object(self.svc, "gateway")
        gateway.start()
        self.addCleanup(gateway.stop)
        self.marker = Path(tmp.name) / "infra" / "docker.reattach-pending"

    def _ensure(self, restarted: bool, **kwargs: Any) -> str:
        with patch.object(self.svc, "_bring_up", return_value=("http://orch", restarted)):
            return self.svc.ensure_running(**kwargs)

    def test_adopted_pair_does_not_reattach(self) -> None:
        self.assertEqual("http://orch", self._ensure(False))
        self.reattach.assert_not_called()

    def test_restarted_pair_reattaches_and_clears_the_marker(self) -> None:
        self._ensure(True)
        self.assertEqual("http://orch", self.reattach.call_args.kwargs["orchestrator_url"])
        self.assertFalse(self.marker.exists())

    def test_a_failed_reattach_is_retried_even_on_adopt(self) -> None:
        self.reattach.side_effect = GatewayAttachError("could not reattach 1 bottle")
        with patch("bot_bottle.backend.infra_service.die", side_effect=SystemExit) as die, \
             self.assertRaises(SystemExit):
            self._ensure(True)
        self.assertIn("could not reattach 1 bottle", die.call_args.args[0])
        self.assertTrue(self.marker.exists())
        self.reattach.side_effect = None
        self._ensure(False)  # adopted, but the marker says it never finished
        self.assertEqual(2, self.reattach.call_count)
        self.assertFalse(self.marker.exists())

    def _changed_settings(self, *, healthy: bool) -> MagicMock:
        """Record a fingerprint other than the service's, then ensure."""
        self.svc._record_infra_settings()  # pyright: ignore[reportPrivateUsage]
        record = self.marker.parent / "docker.settings.json"
        record.write_text(record.read_text().replace(self.svc.settings.infra.fingerprint(), "older"))
        with patch.object(self.svc, "is_healthy", return_value=healthy), \
             patch.object(self.svc, "stop") as stop:
            self._ensure(True)
        return stop

    def test_changed_infra_settings_restart_running_infra(self) -> None:
        self._changed_settings(healthy=True).assert_called_once()

    def test_changed_infra_settings_leave_stopped_infra_alone(self) -> None:
        self._changed_settings(healthy=False).assert_not_called()

    def test_unchanged_or_unrecorded_settings_never_stop_the_pair(self) -> None:
        with patch.object(self.svc, "is_healthy", return_value=True), \
             patch.object(self.svc, "stop") as stop:
            self._ensure(True)   # nothing recorded yet; this records it
            self._ensure(False)  # recorded and unchanged
        stop.assert_not_called()

    def test_a_bring_up_interrupted_mid_restart_is_retried(self) -> None:
        # Interrupted after a plane was recreated but before `_bring_up`
        # returned: nothing reported the restart, so the marker must already
        # be down for the next (adopting) bring-up to reattach.
        with patch.object(self.svc, "_bring_up", side_effect=KeyboardInterrupt), \
             self.assertRaises(KeyboardInterrupt):
            self.svc.ensure_running()
        self.assertTrue(self.marker.exists())
        self._ensure(False)
        self.reattach.assert_called_once()
        self.assertFalse(self.marker.exists())

    def test_bring_up_holds_the_lock_and_may_re_enter_it(self) -> None:
        from bot_bottle.backend import infra_service
        lock = Path(self.marker.parent) / "docker.lock"

        def nested(**_: Any) -> tuple[str, bool]:
            self.assertEqual(1, infra_service._HELD.get(lock))
            with self.svc.bring_up_lock():  # e.g. an image build bringing infra up
                self.assertEqual(2, infra_service._HELD.get(lock))
            return "http://orch", False

        with patch.object(self.svc, "_bring_up", side_effect=nested):
            self.svc.ensure_running()
        self.assertNotIn(lock, infra_service._HELD)


def _done(rc: int = 0, out: str = "", err: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess([], rc, out, err)


class FirecrackerAttacher(unittest.TestCase):
    def test_lists_live_run_dirs_by_guest_ip(self) -> None:
        from bot_bottle.backend.firecracker import gateway_attach as fga
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "slug-a"
            run_dir.mkdir()
            (run_dir / "config.json").write_text(json.dumps(
                {"boot-source": {"boot_args": "console=ttyS0 ip=10.243.0.2::10.243.0.1:255"}}))
            (run_dir / "bottle_id_ed25519").write_text("key")
            with patch.object(fga.cleanup, "live_run_dirs", return_value=(run_dir,)):
                bottles = fga.FirecrackerGatewayAttacher().running_bottles()
        self.assertEqual([RunningBottle(str(run_dir), "slug-a", "10.243.0.2")], bottles)

    def test_live_vm_without_its_key_fails_hard(self) -> None:
        from bot_bottle.backend.firecracker import gateway_attach as fga
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(fga.cleanup, "live_run_dirs", return_value=(Path(tmp),)), \
             self.assertRaises(GatewayAttachError):
            fga.FirecrackerGatewayAttacher().running_bottles()

    def test_pointing_at_the_gateway_touches_nothing(self) -> None:
        # Agent VMs dial their slot's fixed TAP address, DNAT'd to the gateway.
        from bot_bottle.backend.firecracker import gateway_attach as fga
        with patch.object(fga.subprocess, "run") as run:
            fga.FirecrackerGatewayAttacher().point_at_gateway(_A, "10.243.255.3")
        run.assert_not_called()

    def test_ca_goes_over_stdin_and_failures_raise(self) -> None:
        from bot_bottle.backend.firecracker import gateway_attach as fga
        with patch.object(fga.subprocess, "run", return_value=_done()) as run:
            fga.FirecrackerGatewayAttacher().install_ca(_A, "PEM")
        self.assertEqual("PEM", run.call_args.kwargs["input"])
        self.assertIn("update-ca-certificates", run.call_args.args[0][-1])
        with patch.object(fga.subprocess, "run", return_value=_done(255, err="refused\x1b[2J")), \
             self.assertRaises(GatewayAttachError) as raised:
            fga.FirecrackerGatewayAttacher().install_ca(_A, "PEM")
        self.assertNotIn("\x1b", str(raised.exception))  # no terminal escapes


class DockerAttacher(unittest.TestCase):
    def test_lists_agent_containers_by_network_address(self) -> None:
        from bot_bottle.backend.docker import gateway_attach as dga

        def run(argv: list[str], **_: Any) -> "subprocess.CompletedProcess[str]":
            if argv[1] == "ps":
                return _done(out=f"c0ffee-{argv[-3].rsplit('-', 1)[-1]}\n")
            if argv[-1] == "c0ffee-a":  # its own agent network
                return _done(out="bot-bottle-agent-a=10.242.0.3 \n")
            return _done(out="net=172.30.0.5 \n")  # launched on the shared one

        with patch.object(dga, "list_active_slugs", return_value=["a", "b"]), \
             patch.object(dga, "run_docker", side_effect=run):
            bottles = dga.DockerGatewayAttacher("net").running_bottles()
        self.assertEqual([RunningBottle("c0ffee-a", "a", "10.242.0.3"),
                          RunningBottle("c0ffee-b", "b", "172.30.0.5")], bottles)

    def test_the_gateway_rejoins_the_bottles_own_network_at_its_address(self) -> None:
        from bot_bottle.backend.docker import gateway_attach as dga
        env = json.dumps(["HTTPS_PROXY=http://id:tok@10.242.0.10:9099"])
        calls: list[list[str]] = []

        def run(argv: list[str], **_: Any) -> "subprocess.CompletedProcess[str]":
            calls.append(argv)
            if "Config.Env" in " ".join(argv):
                return _done(out=env)
            if argv[1] == "inspect":
                return _done(out="bot-bottle-agent-a=10.242.0.11 \n")
            return _done()

        def network(argv: list[str], **_: Any) -> "subprocess.CompletedProcess[str]":
            calls.append(argv)
            return _done(out="1|true|10.242.0.8/29\n")

        with patch.object(dga, "run_docker", side_effect=run), \
             patch("bot_bottle.backend.docker.agent_network.run_docker", side_effect=network):
            # The shared network's address doesn't apply to it.
            dga.DockerGatewayAttacher("net", "gw").point_at_gateway(_A, "10.242.255.2")
        self.assertIn(["docker", "network", "connect", "--ip", "10.242.0.10",
                       "bot-bottle-agent-a", "gw"], calls)

    def test_ca_installs_as_root_without_the_agents_path(self) -> None:
        # The agent's PATH can include dirs it writes to: an absolute shell and
        # a pinned system PATH keep anything it planted from running as root.
        from bot_bottle.backend.docker import gateway_attach as dga
        with patch.object(dga, "run_docker", return_value=_done()) as run:
            dga.DockerGatewayAttacher().install_ca(_A, "PEM")
        argv = run.call_args.args[0]
        self.assertEqual(["docker", "exec", "-u", "0", "-i", "ref-a", "/bin/sh", "-c"], argv[:8])
        self.assertTrue(argv[8].startswith(f"export PATH={ROOT_EXEC_PATH};"))
        self.assertEqual("PEM", run.call_args.kwargs["input"])
        self.assertIsNotNone(run.call_args.kwargs["timeout"])

    def test_secret_is_read_from_the_container_config_not_by_exec(self) -> None:
        from bot_bottle.backend.docker import gateway_attach as dga
        env = json.dumps(["PATH=/x", "ENV_VAR_SECRET=s3cret"])
        with patch.object(dga, "run_docker", return_value=_done(out=env)) as run:
            self.assertEqual("s3cret", dga.DockerGatewayAttacher().read_env_var_secret(_A))
        self.assertEqual("inspect", run.call_args.args[0][1])

    def test_bottle_proxying_through_the_gateway_address_is_confirmed(self) -> None:
        from bot_bottle.backend.docker import gateway_attach as dga
        env = json.dumps(["HTTPS_PROXY=http://id:tok@10.242.255.2:9099"])
        with patch.object(dga, "run_docker", return_value=_done(out=env)):
            dga.DockerGatewayAttacher().point_at_gateway(_A, "10.242.255.2")

    def test_bottle_proxying_elsewhere_fails_without_leaking_its_token(self) -> None:
        # Its environ can't be rewritten, so a moved gateway is a loud failure.
        from bot_bottle.backend.docker import gateway_attach as dga
        env = json.dumps(["HTTPS_PROXY=http://id:tok@10.242.255.7:9099"])
        with patch.object(dga, "run_docker", return_value=_done(out=env)), \
             self.assertRaises(GatewayAttachError) as raised:
            dga.DockerGatewayAttacher().point_at_gateway(_A, "10.242.255.2")
        self.assertIn("10.242.255.7", str(raised.exception))
        self.assertNotIn("tok", str(raised.exception))

    def test_unreadable_config_raises(self) -> None:
        from bot_bottle.backend.docker import gateway_attach as dga
        with patch.object(dga, "run_docker", return_value=_done(1, err="no such container")), \
             self.assertRaises(GatewayAttachError):
            dga.DockerGatewayAttacher().read_env_var_secret(_A)

    def test_exec_timeout_raises(self) -> None:
        from bot_bottle.backend.docker import gateway_attach as dga
        with patch.object(dga, "run_docker",
                          side_effect=subprocess.TimeoutExpired("docker", 60)), \
             self.assertRaises(GatewayAttachError):
            dga.DockerGatewayAttacher().install_ca(_A, "PEM")


class MacosAttacher(unittest.TestCase):
    def test_ca_is_staged_in_root_only_dir_then_installed(self) -> None:
        from bot_bottle.backend.macos_container import gateway_attach as mga
        with patch.object(mga.container_mod, "run_container_argv", return_value=_done()) as run:
            mga.MacosGatewayAttacher("net").install_ca(_A, "PEM")
        cp, exec_ = (c.args[0] for c in run.call_args_list)
        self.assertEqual(["container", "cp"], cp[:2])
        self.assertTrue(cp[3].startswith("ref-a:/root/"))  # not world-writable /tmp
        self.assertEqual(["container", "exec", "--user", "root", "ref-a", "/bin/sh", "-c"],
                         exec_[:7])
        self.assertTrue(exec_[7].startswith(f"export PATH={ROOT_EXEC_PATH};"))

    def test_gateway_name_is_repointed_as_root_without_tmp_or_the_agents_path(self) -> None:
        from bot_bottle.backend.macos_container import gateway_attach as mga
        with patch.object(mga.container_mod, "run_container_argv",
                          return_value=_done(out="bb-firewall=ok\n")) as run:
            mga.MacosGatewayAttacher("net").point_at_gateway(_A, "192.168.129.7")
        argv = run.call_args.args[0]
        self.assertEqual(["container", "exec", "--user", "root", "ref-a", "/bin/sh", "-c"],
                         argv[:7])
        self.assertTrue(argv[7].startswith(f"export PATH={ROOT_EXEC_PATH};"))
        self.assertIn("192.168.129.7 ", argv[7].replace("'", ""))
        self.assertNotIn("/tmp", argv[7])
        with patch.object(mga.container_mod, "run_container_argv",
                          return_value=_done(1, err="exited")), \
             self.assertRaises(GatewayAttachError):
            mga.MacosGatewayAttacher("net").point_at_gateway(_A, "192.168.129.7")

    def test_secret_comes_from_the_container_config(self) -> None:
        from bot_bottle.backend.macos_container import gateway_attach as mga
        inspect = json.dumps([{"configuration": {"initProcess": {
            "environment": ["HOME=/home/node", "ENV_VAR_SECRET=s3cret"]}}}])
        with patch.object(mga.container_mod, "run_container_argv",
                          return_value=_done(out=inspect)):
            self.assertEqual("s3cret", mga.MacosGatewayAttacher("net").read_env_var_secret(_A))

    def test_uninspectable_container_fails_hard(self) -> None:
        from bot_bottle.backend.macos_container import gateway_attach as mga
        agent = MagicMock(slug="a")
        with patch.object(mga, "enumerate_active", return_value=[agent]), \
             patch.object(mga.container_mod, "inspect_container_network_ip", return_value=None), \
             self.assertRaises(GatewayAttachError):
            mga.MacosGatewayAttacher("net").running_bottles()


if __name__ == "__main__":
    unittest.main()
