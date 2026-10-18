"""PRD 0081: reattaching running bottles to a freshly (re)started gateway."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from bot_bottle.backend import gateway_attach as ga
from bot_bottle.backend.gateway_attach import (
    AttachPolicy, GatewayAttachError, GatewayAttacher, RunningBottle,
)
from bot_bottle.gateway import GatewayError

_A = RunningBottle(ref="ref-a", slug="a", source_ip="10.0.0.2")
_B = RunningBottle(ref="ref-b", slug="b", source_ip="10.0.0.3")


class _Attacher(GatewayAttacher):
    def __init__(self, bottles: list[RunningBottle], *, fail_ca: set[str] | None = None,
                 secrets: dict[str, str] | None = None) -> None:
        self.bottles = bottles
        self.fail_ca = fail_ca or set()
        self.secrets = secrets or {}
        self.installed: list[tuple[str, str]] = []

    def running_bottles(self) -> list[RunningBottle]:
        return self.bottles

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
        tokens = patch.object(ga, "reprovision_bottles")
        self.tokens = tokens.start()
        self.addCleanup(tokens.stop)

    def _run(self, attacher: GatewayAttacher, policy: AttachPolicy = AttachPolicy.STRICT) -> None:
        ga.reattach_running_bottles(
            attacher, orchestrator_url="http://orch", gateway=self.gateway, policy=policy)

    def test_restores_ca_git_gate_and_egress_tokens_for_every_bottle(self) -> None:
        attacher = _Attacher([_A, _B], secrets={"a": "sa", "b": "sb"})
        self._run(attacher)
        self.assertEqual([("a", "PEM"), ("b", "PEM")], attacher.installed)
        transport = self.gateway.provisioning_transport.return_value
        self.git_gate.assert_any_call(transport, "id-a", "a")
        self.git_gate.assert_any_call(transport, "id-b", "b")
        self.assertEqual({"10.0.0.2": "sa", "10.0.0.3": "sb"}, self.tokens.call_args.args[1])

    def test_no_running_bottles_touches_nothing(self) -> None:
        self._run(_Attacher([]))
        self.gateway.ca_cert_pem.assert_not_called()
        self.client.list_bottles.assert_not_called()

    def test_every_bottle_is_attempted_and_failures_raised_together(self) -> None:
        attacher = _Attacher([_A, _B], fail_ca={"a"}, secrets={"b": "sb"})
        with self.assertRaises(GatewayAttachError) as raised:
            self._run(attacher)
        self.assertIn("a: ssh: connection refused", str(raised.exception))
        self.assertEqual([("b", "PEM")], attacher.installed)  # b still reattached
        self.assertEqual({"10.0.0.3": "sb"}, self.tokens.call_args.args[1])

    def test_report_policy_warns_and_continues(self) -> None:
        with patch.object(ga, "warn") as warn:
            self._run(_Attacher([_A], fail_ca={"a"}), AttachPolicy.REPORT)
        self.assertIn("could not reattach 1", warn.call_args.args[0])

    def test_unregistered_bottle_is_a_failure(self) -> None:
        self.client.list_bottles.return_value = [{"bottle_id": "id-a", "source_ip": "10.0.0.2"}]
        with self.assertRaises(GatewayAttachError) as raised:
            self._run(_Attacher([_A, _B]))
        self.assertIn("b: running but not registered", str(raised.exception))

    def test_unreachable_gateway_fails_before_any_bottle(self) -> None:
        self.gateway.ca_cert_pem.side_effect = GatewayError("no CA yet")
        attacher = _Attacher([_A])
        with self.assertRaises(GatewayAttachError):
            self._run(attacher)
        self.assertEqual([], attacher.installed)


class ReprovisionGitGate(unittest.TestCase):
    transport = MagicMock()

    def test_rebuilds_the_plan_from_the_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "upstreams.json").write_text(json.dumps([
                {"name": "repo", "upstream_url": "ssh://git@forge/o/r.git",
                 "upstream_host": "forge", "upstream_port": "22",
                 "identity_file": "/manifest/key", "known_host_key": "ssh-ed25519 AAA"},
            ]))
            (state / "repo-key").write_text("deploy key")
            (state / "git_gate_pre_receive.sh").write_text("stale hook")
            with patch.object(ga, "git_gate_state_dir", return_value=state), \
                 patch.object(ga, "provision_git_gate") as provision:
                ga.reprovision_git_gate(self.transport, "id-a", "a")
            hook_text = (state / "git_gate_pre_receive.sh").read_text()
            access_mode = (state / "git_gate_access_hook.sh").stat().st_mode
        transport, bottle_id, plan = provision.call_args.args
        self.assertEqual((self.transport, "id-a"), (transport, bottle_id))
        self.assertEqual(str(state / "repo-key"), plan.upstreams[0].identity_file)
        self.assertEqual(state / "git_gate_gitleaks.toml", plan.gitleaks_config)
        # Hooks come from the current code, not the bottle's stale staged copy.
        self.assertEqual(ga.git_gate_render_hook(), hook_text)
        self.assertEqual(0o700, access_mode & 0o777)

    def test_no_snapshot_means_no_git_upstreams(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(ga, "git_gate_state_dir", return_value=Path(tmp)), \
             patch.object(ga, "provision_git_gate") as provision:
            ga.reprovision_git_gate(self.transport, "id-a", "a")
        provision.assert_not_called()


class InfraServiceReattach(unittest.TestCase):
    def test_failure_ends_the_bring_up_with_the_reasons(self) -> None:
        from bot_bottle.backend.docker.infra import DockerInfraService
        svc = DockerInfraService(port=8099)
        with patch("bot_bottle.backend.infra_service.reattach_running_bottles",
                   side_effect=GatewayAttachError("could not reattach 1 bottle")), \
             patch.object(svc, "gateway"), \
             patch("bot_bottle.backend.infra_service.die", side_effect=SystemExit) as die, \
             self.assertRaises(SystemExit):
            svc.reattach_running_bottles("http://orch")
        self.assertIn("could not reattach 1 bottle", die.call_args.args[0])


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

    def test_ca_goes_over_stdin_and_failures_raise(self) -> None:
        from bot_bottle.backend.firecracker import gateway_attach as fga
        with patch.object(fga.subprocess, "run", return_value=_done()) as run:
            fga.FirecrackerGatewayAttacher().install_ca(_A, "PEM")
        self.assertEqual("PEM", run.call_args.kwargs["input"])
        self.assertIn("update-ca-certificates", run.call_args.args[0][-1])
        with patch.object(fga.subprocess, "run", return_value=_done(255, err="refused")), \
             self.assertRaises(GatewayAttachError):
            fga.FirecrackerGatewayAttacher().install_ca(_A, "PEM")


class DockerAttacher(unittest.TestCase):
    def test_lists_agent_containers_by_network_address(self) -> None:
        from bot_bottle.backend.docker import gateway_attach as dga

        def run(argv: list[str], **_: Any) -> "subprocess.CompletedProcess[str]":
            if argv[1] == "ps":
                return _done(out="c0ffee\n")
            return _done(out="172.30.0.5\n")

        with patch.object(dga, "list_active_slugs", return_value=["a"]), \
             patch.object(dga, "run_docker", side_effect=run):
            bottles = dga.DockerGatewayAttacher("net").running_bottles()
        self.assertEqual([RunningBottle("c0ffee", "a", "172.30.0.5")], bottles)

    def test_ca_installs_as_root_over_stdin(self) -> None:
        from bot_bottle.backend.docker import gateway_attach as dga
        with patch.object(dga, "run_docker", return_value=_done()) as run:
            dga.DockerGatewayAttacher().install_ca(_A, "PEM")
        argv = run.call_args.args[0]
        self.assertEqual(["docker", "exec", "-u", "0", "-i", "ref-a"], argv[:6])
        self.assertEqual("PEM", run.call_args.kwargs["input"])

    def test_unset_secret_is_empty_not_an_error(self) -> None:
        from bot_bottle.backend.docker import gateway_attach as dga
        with patch.object(dga, "run_docker", return_value=_done(1)):
            self.assertEqual("", dga.DockerGatewayAttacher().read_env_var_secret(_A))


class MacosAttacher(unittest.TestCase):
    def test_ca_is_copied_in_then_installed_as_root(self) -> None:
        from bot_bottle.backend.macos_container import gateway_attach as mga
        with patch.object(mga.container_mod, "run_container_argv", return_value=_done()) as run:
            mga.MacosGatewayAttacher("net").install_ca(_A, "PEM")
        cp, exec_ = (c.args[0] for c in run.call_args_list)
        self.assertEqual(["container", "cp"], cp[:2])
        self.assertEqual(["container", "exec", "--user", "root", "ref-a"], exec_[:5])

    def test_uninspectable_container_fails_hard(self) -> None:
        from bot_bottle.backend.macos_container import gateway_attach as mga
        agent = MagicMock(slug="a")
        with patch.object(mga, "enumerate_active", return_value=[agent]), \
             patch.object(mga.container_mod, "inspect_container_network_ip", return_value=None), \
             self.assertRaises(GatewayAttachError):
            mga.MacosGatewayAttacher("net").running_bottles()


if __name__ == "__main__":
    unittest.main()
