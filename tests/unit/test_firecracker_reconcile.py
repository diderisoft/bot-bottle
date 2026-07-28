"""Unit: bring-up reconcile — CA push, git-gate reprovision, egress tokens.

Covers attach_bottled_agents_to_gateway (reconcile.py): each per-bottle step
fires with correct args, failures on one bottle don't block others, and the
egress-token restore path works end-to-end. Does NOT spin up VMs or real SSH.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess
from unittest.mock import MagicMock, patch

_RECONCILE = "bot_bottle.backend.firecracker.reconcile"


class TestGuestIpFromConfig(unittest.TestCase):
    def test_reads_ip_from_boot_args(self) -> None:
        from bot_bottle.backend.firecracker.reconcile import _guest_ip_from_config
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "boot-source": {"boot_args": "console=ttyS0 ip=10.243.0.5:10.243.0.6:255.255.255.254"},
            }, f)
            name = f.name
        self.assertEqual("10.243.0.5", _guest_ip_from_config(Path(name)))

    def test_returns_empty_on_missing_file(self) -> None:
        from bot_bottle.backend.firecracker.reconcile import _guest_ip_from_config
        self.assertEqual("", _guest_ip_from_config(Path("/nonexistent/config.json")))

    def test_returns_empty_on_bad_json(self) -> None:
        from bot_bottle.backend.firecracker.reconcile import _guest_ip_from_config
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not-json")
            name = f.name
        self.assertEqual("", _guest_ip_from_config(Path(name)))


class TestPushCa(unittest.TestCase):
    def test_runs_three_ssh_commands_in_order(self) -> None:
        from bot_bottle.backend.firecracker.reconcile import _push_ca
        with tempfile.TemporaryDirectory() as td:
            key = Path(td) / "key"
            key.write_text("k")
            with patch(f"{_RECONCILE}.subprocess.run") as run:
                run.return_value = CompletedProcess([], 0)
                _push_ca(key, "10.0.0.1", "-----BEGIN CERTIFICATE-----\n")
        calls = run.call_args_list
        self.assertEqual(3, len(calls))
        # Each call's argv is a list; the remote command is the last element.
        self.assertIn("mkdir", calls[0].args[0][-1])
        self.assertIn("cat >", calls[1].args[0][-1])
        self.assertIn("update-ca-certificates", calls[2].args[0][-1])

    def test_passes_ca_pem_via_stdin(self) -> None:
        from bot_bottle.backend.firecracker.reconcile import _push_ca
        with tempfile.TemporaryDirectory() as td:
            key = Path(td) / "key"
            key.write_text("k")
            with patch(f"{_RECONCILE}.subprocess.run") as run:
                run.return_value = CompletedProcess([], 0)
                _push_ca(key, "10.0.0.1", "MY-CA-PEM")
        cat_call = run.call_args_list[1]
        self.assertEqual("MY-CA-PEM", cat_call.kwargs.get("input"))

    def test_raises_on_ssh_failure(self) -> None:
        from bot_bottle.backend.firecracker.reconcile import _push_ca
        with tempfile.TemporaryDirectory() as td:
            key = Path(td) / "key"
            key.write_text("k")
            with patch(f"{_RECONCILE}.subprocess.run") as run:
                run.side_effect = CalledProcessError(1, "ssh")
                with self.assertRaises(CalledProcessError):
                    _push_ca(key, "10.0.0.1", "pem")


class TestReprovisionGitGate(unittest.TestCase):
    def _make_state_dir(self, upstreams: list[dict[str, str]]) -> Path:
        d = Path(tempfile.mkdtemp())
        (d / "upstreams.json").write_text(json.dumps(upstreams))
        (d / "git_gate_pre_receive.sh").write_text("#!/bin/sh")
        (d / "git_gate_access_hook.sh").write_text("#!/bin/sh")
        (d / "git_gate_entrypoint.sh").write_text("#!/bin/sh")
        return d

    def test_no_op_when_no_upstreams_json(self) -> None:
        from bot_bottle.backend.firecracker.reconcile import _reprovision_git_gate
        transport = MagicMock()
        with tempfile.TemporaryDirectory() as td:
            # The state dir exists but has no upstreams.json
            with patch(f"{_RECONCILE}.git_gate_state_dir", return_value=Path(td)):
                _reprovision_git_gate(transport, "bottle-123", "my-slug")
        transport.exec.assert_not_called()

    def test_calls_provision_git_gate_with_upstreams(self) -> None:
        from bot_bottle.backend.firecracker.reconcile import _reprovision_git_gate
        transport = MagicMock()
        upstreams = [{
            "name": "bot-bottle",
            "upstream_url": "ssh://git@gitea.example/org/bot-bottle.git",
            "upstream_host": "gitea.example",
            "upstream_port": "22",
            "identity_file": "/home/node/.ssh/id_ed25519",
            "known_host_key": "ssh-ed25519 AAAA...",
        }]
        state_dir = self._make_state_dir(upstreams)
        # Write the key file so identity_file falls back to state dir path.
        (state_dir / "bot-bottle-key").write_bytes(b"PRIVATE")
        try:
            with patch(f"{_RECONCILE}.git_gate_state_dir", return_value=state_dir), \
                    patch(f"{_RECONCILE}.provision_git_gate") as prov:
                _reprovision_git_gate(transport, "bottle-abc", "my-slug")
            prov.assert_called_once()
            _, bottle_id, plan = prov.call_args.args
            self.assertEqual("bottle-abc", bottle_id)
            self.assertEqual(1, len(plan.upstreams))
            self.assertEqual("bot-bottle", plan.upstreams[0].name)
            # Key in state dir takes priority over identity_file in JSON.
            self.assertEqual(str(state_dir / "bot-bottle-key"), plan.upstreams[0].identity_file)
        finally:
            import shutil; shutil.rmtree(state_dir, ignore_errors=True)

    def test_falls_back_to_manifest_identity_file_when_no_key_in_state(self) -> None:
        from bot_bottle.backend.firecracker.reconcile import _reprovision_git_gate
        transport = MagicMock()
        upstreams = [{
            "name": "repo",
            "upstream_url": "ssh://git@github.com/org/repo.git",
            "upstream_host": "github.com",
            "upstream_port": "22",
            "identity_file": "/home/node/.ssh/id_ed25519",
            "known_host_key": "",
        }]
        state_dir = self._make_state_dir(upstreams)
        # No `repo-key` in state dir — static manifest path should be used.
        try:
            with patch(f"{_RECONCILE}.git_gate_state_dir", return_value=state_dir), \
                    patch(f"{_RECONCILE}.provision_git_gate") as prov:
                _reprovision_git_gate(transport, "bottle-xyz", "my-slug")
            prov.assert_called_once()
            _, _, plan = prov.call_args.args
            self.assertEqual("/home/node/.ssh/id_ed25519", plan.upstreams[0].identity_file)
        finally:
            import shutil; shutil.rmtree(state_dir, ignore_errors=True)


class TestAttachBottledAgentsToGateway(unittest.TestCase):
    """Integration-level: the full reconcile loop against mocked SSH + gateway."""

    def _make_run_dir(self, tmp: Path, slug: str, guest_ip: str) -> Path:
        run_dir = tmp / slug
        run_dir.mkdir()
        key = run_dir / "bottle_id_ed25519"
        key.write_text("PRIVATE")
        cfg = {
            "boot-source": {
                "boot_args": f"console=ttyS0 ip={guest_ip}:{guest_ip}:255.255.255.254",
            }
        }
        (run_dir / "config.json").write_text(json.dumps(cfg))
        return run_dir

    def _make_gateway(self, ca_pem: str = "-----BEGIN CERTIFICATE-----\n") -> MagicMock:
        gw = MagicMock()
        gw.ca_cert_pem.return_value = ca_pem
        gw.provisioning_transport.return_value = MagicMock()
        return gw

    def test_skips_all_when_ca_fetch_fails(self) -> None:
        from bot_bottle.backend.firecracker.gateway import FirecrackerGateway
        from bot_bottle.backend.firecracker.reconcile import attach_bottled_agents_to_gateway
        from bot_bottle.gateway import GatewayError
        gw = MagicMock(spec=FirecrackerGateway)
        gw.ca_cert_pem.side_effect = GatewayError("timeout")
        with patch(f"{_RECONCILE}.cleanup.live_run_dirs", return_value=()):
            attach_bottled_agents_to_gateway("http://orch:8099", gw)
        # Nothing else is called when CA fetch fails.
        gw.provisioning_transport.assert_not_called()

    def test_ca_push_called_per_live_bottle(self) -> None:
        from bot_bottle.backend.firecracker.reconcile import attach_bottled_agents_to_gateway
        gw = self._make_gateway()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rd1 = self._make_run_dir(tmp, "agent-abc12", "10.243.0.5")
            rd2 = self._make_run_dir(tmp, "agent-xyz99", "10.243.0.7")
            with patch(f"{_RECONCILE}.cleanup.live_run_dirs", return_value=(rd1, rd2)), \
                    patch(f"{_RECONCILE}.OrchestratorClient") as client_cls, \
                    patch(f"{_RECONCILE}._push_ca") as push_ca, \
                    patch(f"{_RECONCILE}._reprovision_git_gate"), \
                    patch(f"{_RECONCILE}.subprocess.run",
                          return_value=CompletedProcess([], 1)):
                client_cls.return_value.list_bottles.return_value = []
                attach_bottled_agents_to_gateway("http://orch:8099", gw)
        # CA pushed to both bottles.
        self.assertEqual(2, push_ca.call_count)

    def test_per_bottle_ca_failure_does_not_block_others(self) -> None:
        from bot_bottle.backend.firecracker.reconcile import attach_bottled_agents_to_gateway
        gw = self._make_gateway()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rd1 = self._make_run_dir(tmp, "agent-fail1", "10.243.0.5")
            rd2 = self._make_run_dir(tmp, "agent-ok99", "10.243.0.7")
            with patch(f"{_RECONCILE}.cleanup.live_run_dirs", return_value=(rd1, rd2)), \
                    patch(f"{_RECONCILE}.OrchestratorClient") as client_cls, \
                    patch(f"{_RECONCILE}._push_ca",
                          side_effect=[CalledProcessError(1, "ssh"), None]) as push_ca, \
                    patch(f"{_RECONCILE}._reprovision_git_gate"), \
                    patch(f"{_RECONCILE}.subprocess.run",
                          return_value=CompletedProcess([], 1)):
                client_cls.return_value.list_bottles.return_value = []
                # Must not raise even though the first bottle's CA push fails.
                attach_bottled_agents_to_gateway("http://orch:8099", gw)
        # Both bottles were attempted.
        self.assertEqual(2, push_ca.call_count)

    def test_egress_token_restored_for_bottles_with_secret(self) -> None:
        from bot_bottle.backend.firecracker.reconcile import attach_bottled_agents_to_gateway
        gw = self._make_gateway()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rd = self._make_run_dir(tmp, "agent-tok11", "10.243.0.5")
            bottles = [{"bottle_id": "bid-001", "source_ip": "10.243.0.5"}]
            with patch(f"{_RECONCILE}.cleanup.live_run_dirs", return_value=(rd,)), \
                    patch(f"{_RECONCILE}.OrchestratorClient") as client_cls, \
                    patch(f"{_RECONCILE}._push_ca"), \
                    patch(f"{_RECONCILE}._reprovision_git_gate"), \
                    patch(f"{_RECONCILE}.reprovision_bottles") as reprov, \
                    patch(f"{_RECONCILE}.subprocess.run",
                          return_value=CompletedProcess([], 0, stdout="secret-key\n")):
                client_cls.return_value.list_bottles.return_value = bottles
                reprov.return_value = 1
                attach_bottled_agents_to_gateway("http://orch:8099", gw)
        reprov.assert_called_once()
        _, secrets = reprov.call_args.args
        self.assertIn("10.243.0.5", secrets)
        self.assertEqual("secret-key", secrets["10.243.0.5"])

    def test_git_gate_reprovision_called_with_bottle_id(self) -> None:
        from bot_bottle.backend.firecracker.reconcile import attach_bottled_agents_to_gateway
        gw = self._make_gateway()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rd = self._make_run_dir(tmp, "agent-git11", "10.243.0.5")
            bottles = [{"bottle_id": "bid-git", "source_ip": "10.243.0.5"}]
            with patch(f"{_RECONCILE}.cleanup.live_run_dirs", return_value=(rd,)), \
                    patch(f"{_RECONCILE}.OrchestratorClient") as client_cls, \
                    patch(f"{_RECONCILE}._push_ca"), \
                    patch(f"{_RECONCILE}._reprovision_git_gate") as reprov_gw, \
                    patch(f"{_RECONCILE}.subprocess.run",
                          return_value=CompletedProcess([], 1)):
                client_cls.return_value.list_bottles.return_value = bottles
                attach_bottled_agents_to_gateway("http://orch:8099", gw)
        reprov_gw.assert_called_once()
        _, bottle_id_arg, slug_arg = reprov_gw.call_args.args
        self.assertEqual("bid-git", bottle_id_arg)
        self.assertEqual("agent-git11", slug_arg)

    def test_run_dir_without_key_file_is_skipped(self) -> None:
        from bot_bottle.backend.firecracker.reconcile import attach_bottled_agents_to_gateway
        gw = self._make_gateway()
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rd = self._make_run_dir(tmp, "agent-nokey", "10.243.0.5")
            (rd / "bottle_id_ed25519").unlink()  # remove the key
            with patch(f"{_RECONCILE}.cleanup.live_run_dirs", return_value=(rd,)), \
                    patch(f"{_RECONCILE}.OrchestratorClient") as client_cls, \
                    patch(f"{_RECONCILE}._push_ca") as push_ca, \
                    patch(f"{_RECONCILE}._reprovision_git_gate"):
                client_cls.return_value.list_bottles.return_value = []
                attach_bottled_agents_to_gateway("http://orch:8099", gw)
        push_ca.assert_not_called()


if __name__ == "__main__":
    unittest.main()
