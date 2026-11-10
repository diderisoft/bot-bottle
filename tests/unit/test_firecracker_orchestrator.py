"""Unit: the Firecracker orchestrator (control plane) microVM lifecycle (PRD 0070).

The control plane's host-side logic — boot on the orchestrator link with the
registry volume, seed the signing key, wait for /health — lives in
`FirecrackerOrchestrator`, composing the shared `infra_vm` VM substrate.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.contract.hostctl import Settings
from bot_bottle.backend.firecracker import infra_vm
from bot_bottle.backend.firecracker.orchestrator import (
    ORCHESTRATOR_NAME,
    FirecrackerOrchestrator,
    build_store_volume_path,
    registry_volume_path,
)
from bot_bottle.backend.firecracker.infra_vm import ORCHESTRATOR_PORT

_ORCH = "bot_bottle.backend.firecracker.orchestrator"


class TestUrls(unittest.TestCase):
    def test_url_and_gateway_url_are_the_orch_link_guest_ip(self) -> None:
        orch = FirecrackerOrchestrator(Settings())
        ip = infra_vm.netpool.from_settings(Settings().infra).orch_slot().guest_ip
        self.assertEqual(f"http://{ip}:{ORCHESTRATOR_PORT}", orch.url())
        self.assertEqual(orch.url(), orch.gateway_url())

    def test_ssh_target_is_stable_key_and_orch_guest_ip(self) -> None:
        orch = FirecrackerOrchestrator(Settings())
        with patch.object(infra_vm, "_infra_dir", return_value=Path("/infra")):
            key, ip = orch.ssh_target()
        self.assertEqual(Path("/infra/id_ed25519"), key)
        self.assertEqual(infra_vm.netpool.from_settings(Settings().infra).orch_slot().guest_ip, ip)


class TestEnsureRunning(unittest.TestCase):
    def test_boots_orchestrator_role_with_mem_and_registry_then_pushes_key(self) -> None:
        orch = FirecrackerOrchestrator(Settings())
        vm = infra_vm.InfraVm(guest_ip="10.243.255.1", private_key=Path("/k"), vm=MagicMock())
        with patch.object(infra_vm, "boot_vm", return_value=vm) as boot, \
                patch.object(infra_vm, "push_secret") as push, \
                patch("bot_bottle.trust_domain.host_signing_key", return_value="host-key"), \
                patch.object(orch, "is_running", return_value=False), \
                patch.object(orch, "_ensure_registry_volume", return_value=Path("/reg")), \
                patch.object(orch, "_ensure_build_store_volume", return_value=Path("/bs")), \
                patch.object(orch, "_wait_for_health"):
            orch.ensure_running()
        kw = boot.call_args.kwargs
        self.assertEqual("orchestrator", kw["role"])
        self.assertEqual(4096, kw["mem_mib"])          # orchestrator keeps build headroom
        # DB volume (/dev/vdb), then the buildah store (/dev/vdc).
        self.assertEqual((Path("/reg"), Path("/bs")), kw["data_drives"])
        # The host-canonical signing key is pushed to the guest signing-key path.
        self.assertEqual("host-key", push.call_args_list[0].args[1])
        self.assertEqual(infra_vm._GUEST_SIGNING_KEY_PATH, push.call_args_list[0].args[2])
        # Then the host controller file, empty outside the host controller.
        self.assertEqual(("\n\n", infra_vm.GUEST_HOSTCTL_PATH), push.call_args.args[1:3])

    def test_noop_when_running_and_healthy(self) -> None:
        orch = FirecrackerOrchestrator(Settings())
        with patch.object(orch, "is_running", return_value=True), \
                patch.object(orch, "is_healthy", return_value=True), \
                patch.object(infra_vm, "boot_vm") as boot:
            orch.ensure_running()
        boot.assert_not_called()


class TestWaitForHealth(unittest.TestCase):
    def _vm(self, alive: bool = True) -> infra_vm.InfraVm:
        h = MagicMock()
        h.is_alive.return_value = alive
        return infra_vm.InfraVm(vm=h, guest_ip="10.0.0.1", private_key=Path("/k"))

    def test_returns_when_healthy(self) -> None:
        orch = FirecrackerOrchestrator(Settings())
        with patch.object(orch, "health_problem", return_value=None):
            orch._wait_for_health(self._vm(), timeout=5)  # must not raise

    def test_dies_when_vm_exits(self) -> None:
        orch = FirecrackerOrchestrator(Settings())
        vm = self._vm(alive=False)
        assert vm.vm is not None
        vm.vm.process.returncode = 1
        with patch(f"{_ORCH}.firecracker_vm._console_tail", return_value=""), \
                patch(f"{_ORCH}.die", side_effect=SystemExit) as die:
            with self.assertRaises(SystemExit):
                orch._wait_for_health(vm, timeout=5)
        die.assert_called_once()


class TestRegistryVolume(unittest.TestCase):
    def test_reuses_existing_volume(self) -> None:
        orch = FirecrackerOrchestrator(Settings())
        with tempfile.TemporaryDirectory() as td:
            vol = Path(td) / "registry.ext4"
            vol.write_bytes(b"")  # already present
            with patch(f"{_ORCH}.registry_volume_path", return_value=vol), \
                    patch(f"{_ORCH}.subprocess.run") as run:
                out = orch._ensure_registry_volume()
            run.assert_not_called()  # no mke2fs when it exists
            self.assertEqual(vol, out)

    def test_creates_volume_when_missing(self) -> None:
        from subprocess import CompletedProcess
        orch = FirecrackerOrchestrator(Settings())
        with tempfile.TemporaryDirectory() as td:
            vol = Path(td) / "registry.ext4"
            with patch(f"{_ORCH}.registry_volume_path", return_value=vol), \
                    patch(f"{_ORCH}.subprocess.run",
                          return_value=CompletedProcess([], 0)) as run:
                orch._ensure_registry_volume()
            argv = run.call_args.args[0]
            self.assertIn("mke2fs", argv)
            self.assertIn(str(vol), argv)

    def test_registry_volume_is_under_the_root_not_the_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                patch.dict("os.environ", {"BOT_BOTTLE_ROOT": td}), \
                patch.object(infra_vm, "_infra_dir", return_value=Path("/cache/infra")):
            self.assertEqual(Path(td) / "infra" / "registry.ext4", registry_volume_path())
            self.assertEqual(0o700, (Path(td) / "infra").stat().st_mode & 0o777)

    def test_build_store_volume_created_at_its_own_path(self) -> None:
        from subprocess import CompletedProcess
        orch = FirecrackerOrchestrator(Settings())
        with tempfile.TemporaryDirectory() as td:
            with patch.object(infra_vm, "_infra_dir", return_value=Path(td)), \
                    patch(f"{_ORCH}.subprocess.run",
                          return_value=CompletedProcess([], 0)) as run:
                out = orch._ensure_build_store_volume()
                self.assertEqual(build_store_volume_path(), out)
            self.assertEqual(Path(td) / "build-store.ext4", out)
            argv = run.call_args.args[0]
            self.assertIn("mke2fs", argv)
            self.assertIn(str(out), argv)


class TestLifecycle(unittest.TestCase):
    def test_is_running_reads_the_orch_pidfile(self) -> None:
        orch = FirecrackerOrchestrator(Settings())
        with patch.object(infra_vm, "_pidfile_alive", return_value=True) as alive, \
                patch.object(infra_vm, "_orch_dir", return_value=Path("/orch")):
            self.assertTrue(orch.is_running())
        alive.assert_called_once_with(Path("/orch"))

    def test_stop_kills_only_the_orch_pidfile(self) -> None:
        orch = FirecrackerOrchestrator(Settings())
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "vm.pid").write_text("123")
            with patch.object(infra_vm, "_orch_dir", return_value=d), \
                    patch.object(orch, "is_running", return_value=False), \
                    patch.object(infra_vm, "_kill_pidfile") as kill:
                orch.stop()
            kill.assert_called_once_with(d)
            self.assertFalse((d / "vm.pid").exists())

    def test_stop_syncs_the_guest_before_killing(self) -> None:
        orch = FirecrackerOrchestrator(Settings())
        calls: list[str] = []

        def fake_run(argv: list[str], **_: object) -> None:
            calls.append(argv[-1])

        def fake_kill(_d: Path) -> None:
            calls.append("kill")

        with patch.object(orch, "is_running", return_value=True), \
                patch.object(orch, "ssh_target", return_value=(Path("/k"), "10.0.0.1")), \
                patch(f"{_ORCH}.subprocess.run", side_effect=fake_run), \
                patch.object(infra_vm, "_kill_pidfile", side_effect=fake_kill), \
                patch.object(infra_vm, "_orch_dir", return_value=Path("/nonexistent")):
            orch.stop()
        self.assertEqual(["sync", "kill"], calls)

    def test_stop_still_kills_when_sync_hangs(self) -> None:
        from subprocess import TimeoutExpired
        orch = FirecrackerOrchestrator(Settings())
        with patch.object(orch, "is_running", return_value=True), \
                patch.object(orch, "ssh_target", return_value=(Path("/k"), "10.0.0.1")), \
                patch(f"{_ORCH}.subprocess.run", side_effect=TimeoutExpired("ssh", 15)), \
                patch.object(infra_vm, "_kill_pidfile") as kill, \
                patch.object(infra_vm, "_orch_dir", return_value=Path("/nonexistent")):
            orch.stop()
        kill.assert_called_once()

    def test_name_is_the_orchestrator_vm(self) -> None:
        self.assertEqual(ORCHESTRATOR_NAME, FirecrackerOrchestrator(Settings()).name)


if __name__ == "__main__":
    unittest.main()
