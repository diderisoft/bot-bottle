"""Unit: `backend {setup,status,teardown}` across the three backends.

Exercises the branch matrix — NixOS vs systemd vs neither, root vs
non-root, missing binary/daemon/service — so the generic host-setup
paths are covered without a real host. All output is captured; the point
is the control flow + return codes, not the prose.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import unittest
from typing import Callable
from unittest.mock import MagicMock, patch

from bot_bottle.contract.hostctl import Settings
from bot_bottle.backend.docker import setup as dk
from bot_bottle.contract.hostctl import InfraSettings, Settings
from bot_bottle.backend.firecracker import netpool
from bot_bottle.backend.firecracker import setup as fc
from bot_bottle.backend.macos_container import setup as mc


_S = Settings()
_POOL = netpool.from_settings(_S.infra)


def _cap(fn: Callable[[], int | None]) -> tuple[int | None, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = fn()
    return rc, buf.getvalue()


def _ok(stdout: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


# --- firecracker ----------------------------------------------------

class TestFirecrackerPrereqs(unittest.TestCase):
    def test_binary_found_and_kvm(self):
        with patch.object(fc.shutil, "which", return_value="/usr/bin/firecracker"), \
             patch.object(fc.util, "is_host_capable", return_value=True):
            _, out = _cap(fc._print_prereqs)
        self.assertIn("firecracker binary: found", out)
        self.assertIn("KVM: /dev/kvm present", out)

    def test_binary_missing_and_no_kvm(self):
        with patch.object(fc.shutil, "which", return_value=None), \
             patch.object(fc.util, "is_host_capable", return_value=False):
            _, out = _cap(fc._print_prereqs)
        self.assertIn("NOT found on PATH", out)
        self.assertIn("KVM: /dev/kvm missing", out)


class TestFirecrackerSetup(unittest.TestCase):
    def setUp(self):
        self._p = [
            patch.object(fc, "_print_prereqs", lambda: None),
            patch.object(fc, "_warn_overlaps", return_value=None),
        ]
        for p in self._p:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._p])

    def test_nixos_prints_module_import(self):
        with patch.object(fc, "_is_nixos", return_value=True):
            rc, out = _cap(lambda: fc.setup(_S))
        self.assertEqual(0, rc)
        self.assertIn("nixosModules.firecracker-netpool", out)
        self.assertIn("NON-INVASIVE", out)

    def test_systemd_root_installs_unit(self):
        unit = MagicMock()
        with patch.object(fc, "_is_nixos", return_value=False), \
             patch.object(fc, "_has_systemd", return_value=True), \
             patch.object(fc.os, "geteuid", return_value=0), \
             patch.object(fc, "_UNIT_PATH", unit), \
             patch.object(fc.subprocess, "run", return_value=_ok()):
            rc, out = _cap(lambda: fc.setup(_S))
        self.assertEqual(0, rc)
        unit.write_text.assert_called_once()
        self.assertIn("Installed and started", out)

    def test_systemd_root_enable_failure(self):
        unit = MagicMock()
        with patch.object(fc, "_is_nixos", return_value=False), \
             patch.object(fc, "_has_systemd", return_value=True), \
             patch.object(fc.os, "geteuid", return_value=0), \
             patch.object(fc, "_UNIT_PATH", unit), \
             patch.object(fc.subprocess, "run",
                          side_effect=[_ok(), subprocess.CompletedProcess([], 1)]):
            rc, out = _cap(lambda: fc.setup(_S))
        self.assertEqual(0, rc)
        self.assertIn("enable --now` failed", out)

    def test_systemd_nonroot_prints_block(self):
        with patch.object(fc, "_is_nixos", return_value=False), \
             patch.object(fc, "_has_systemd", return_value=True), \
             patch.object(fc.os, "geteuid", return_value=1000):
            rc, out = _cap(lambda: fc.setup(_S))
        self.assertEqual(0, rc)
        self.assertIn("sudo tee", out)
        self.assertIn("systemctl enable --now", out)

    def test_no_systemd_prints_shell(self):
        with patch.object(fc, "_is_nixos", return_value=False), \
             patch.object(fc, "_has_systemd", return_value=False):
            rc, out = _cap(lambda: fc.setup(_S))
        self.assertEqual(0, rc)
        self.assertIn("firecracker-netpool.sh up", out)


class TestFirecrackerWarnOverlaps(unittest.TestCase):
    def test_emits_on_conflict(self):
        conflict = netpool.RouteConflict(dst="10.243.0.0/24", dev="eth0")
        with patch.object(netpool.NetPool, "overlapping_routes", return_value=[conflict]):
            _, out = _cap(lambda: fc._warn_overlaps(_POOL))
        self.assertIn("overlaps existing routes", out)

    def test_silent_when_clear(self):
        with patch.object(netpool.NetPool, "overlapping_routes", return_value=[]):
            _, out = _cap(lambda: fc._warn_overlaps(_POOL))
        self.assertEqual("", out)


class TestFirecrackerTeardown(unittest.TestCase):
    def test_nixos(self):
        with patch.object(fc, "_is_nixos", return_value=True):
            rc, out = _cap(lambda: fc.teardown(_S))
        self.assertEqual(0, rc)
        self.assertIn("enable = false", out)

    def test_systemd_root_removes_unit(self):
        unit = MagicMock()
        with patch.object(fc, "_is_nixos", return_value=False), \
             patch.object(fc, "_has_systemd", return_value=True), \
             patch.object(fc.os, "geteuid", return_value=0), \
             patch.object(fc, "_UNIT_PATH", unit), \
             patch.object(fc.subprocess, "run", return_value=_ok()):
            rc, out = _cap(lambda: fc.teardown(_S))
        self.assertEqual(0, rc)
        unit.unlink.assert_called_once()
        self.assertIn("removed", out)

    def test_systemd_nonroot_prints(self):
        with patch.object(fc, "_is_nixos", return_value=False), \
             patch.object(fc, "_has_systemd", return_value=True), \
             patch.object(fc.os, "geteuid", return_value=1000):
            rc, out = _cap(lambda: fc.teardown(_S))
        self.assertEqual(0, rc)
        self.assertIn("systemctl disable", out)

    def test_no_systemd(self):
        with patch.object(fc, "_is_nixos", return_value=False), \
             patch.object(fc, "_has_systemd", return_value=False):
            rc, out = _cap(lambda: fc.teardown(_S))
        self.assertEqual(0, rc)
        self.assertIn("firecracker-netpool.sh down", out)


class TestFirecrackerPersistence(unittest.TestCase):
    def test_active(self):
        with patch.object(fc, "_has_systemd", return_value=True), \
             patch.object(fc.subprocess, "run", return_value=_ok("active\n")):
            _, out = _cap(fc._report_persistence)
        self.assertIn("active (survives reboot)", out)

    def test_inactive(self):
        with patch.object(fc, "_has_systemd", return_value=True), \
             patch.object(fc.subprocess, "run", return_value=_ok("inactive\n")):
            _, out = _cap(fc._report_persistence)
        self.assertIn("not installed as a persistent unit", out)

    def test_no_systemd_is_noop(self):
        with patch.object(fc, "_has_systemd", return_value=False):
            _, out = _cap(fc._report_persistence)
        self.assertEqual("", out)


class TestFirecrackerHostDetect(unittest.TestCase):
    def test_is_nixos_via_etc_nixos(self):
        with patch.object(fc.Path, "exists", return_value=True):
            self.assertTrue(fc._is_nixos())

    def test_has_systemd(self):
        with patch.object(fc.Path, "is_dir", return_value=True):
            self.assertTrue(fc._has_systemd())


# --- docker ---------------------------------------------------------

class TestDockerSetupStatus(unittest.TestCase):
    def test_setup_daemon_unreachable(self):
        with patch.object(dk.shutil, "which", return_value="/usr/bin/docker"), \
             patch.object(dk, "_daemon_reachable", return_value=False):
            rc, out = _cap(dk.setup)
        self.assertEqual(1, rc)
        self.assertIn("daemon isn't reachable", out)

    def test_setup_ok_with_runsc_note(self):
        with patch.object(dk.shutil, "which", return_value="/usr/bin/docker"), \
             patch.object(dk, "_daemon_reachable", return_value=True), \
             patch.object(dk._util, "runsc_available", return_value=False):
            rc, out = _cap(dk.setup)
        self.assertEqual(0, rc)
        self.assertIn("gVisor", out)

    def test_status_all_ok(self):
        with patch.object(dk.shutil, "which", return_value="/usr/bin/docker"), \
             patch.object(dk, "_daemon_reachable", return_value=True), \
             patch.object(dk._util, "runsc_available", return_value=True):
            rc, out = _cap(dk.status)
        self.assertEqual(0, rc)
        self.assertIn("registered", out)

    def test_daemon_reachable_false_without_docker(self):
        with patch.object(dk.shutil, "which", return_value=None):
            self.assertFalse(dk._daemon_reachable())

    def test_daemon_reachable_true_when_daemon_responds(self):
        with patch.object(dk.shutil, "which", return_value="/usr/bin/docker"), \
             patch.object(dk.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0)):
            self.assertTrue(dk._daemon_reachable())

    def test_daemon_reachable_false_on_timeout(self):
        with patch.object(dk.shutil, "which", return_value="/usr/bin/docker"), \
             patch.object(dk.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired(["docker", "info"], 5)):
            self.assertFalse(dk._daemon_reachable())

    def test_status_reports_missing_docker(self):
        with patch.object(dk.shutil, "which", return_value=None):
            rc, out = _cap(dk.status)
        self.assertEqual(1, rc)
        self.assertIn("docker on PATH: NO", out)

    def test_setup_missing_docker_prints_install(self):
        with patch.object(dk.shutil, "which", return_value=None):
            rc, out = _cap(dk.setup)
        self.assertEqual(1, rc)
        self.assertIn("docker.com", out)


class TestNetpoolSpanConflict(unittest.TestCase):
    def test_pool_span_bounds(self):
        pool = netpool.from_settings(InfraSettings(fc_ip_base="10.243.0.0", fc_pool_size=8))
        lo, hi = pool.pool_span()
        # base .. base + 2*8 - 1  (16 addresses)
        self.assertEqual(15, hi - lo)

    def test_route_conflict_fields(self):
        c = netpool.RouteConflict(dst="10.243.0.0/24", dev="eth0")
        self.assertEqual("10.243.0.0/24", c.dst)
        self.assertEqual("eth0", c.dev)


# --- macos-container ------------------------------------------------

class TestMacosSetupStatus(unittest.TestCase):
    """As a regular user unless a test says otherwise: CI runs as root, and
    the root path installs the host firewall for real."""

    def setUp(self) -> None:
        self.enterContext(patch.object(mc.os, "geteuid", return_value=501))
        self.enterContext(patch.object(mc._container, "is_macos", return_value=True))
        self.install = self.enterContext(patch.object(mc.host_firewall, "install"))
        self.uninstall = self.enterContext(patch.object(mc.host_firewall, "uninstall"))
        # The host's own interfaces and installed rules don't decide these.
        self.subnet = "192.168.128.0/24"
        self.enterContext(patch.object(mc.host_firewall, "installed_subnet",
                                       return_value=self.subnet))
        self.enterContext(patch.object(mc.host_firewall, "conflicts", return_value=[]))

    def _ready(self, firewall: str = "ok") -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(mc.shutil, "which", return_value="/usr/bin/container"))
        stack.enter_context(patch.object(mc, "_service_running", return_value=True))
        stack.enter_context(patch.object(mc.host_firewall, "state", return_value=firewall))
        return stack

    def test_setup_not_macos(self):
        with patch.object(mc._container, "is_macos", return_value=False):
            rc, out = _cap(mc.setup)
        self.assertEqual(1, rc)
        self.assertIn("requires macOS", out)

    def test_setup_no_cli(self):
        with patch.object(mc.shutil, "which", return_value=None):
            rc, out = _cap(mc.setup)
        self.assertEqual(1, rc)
        self.assertIn("not found on PATH", out)

    def test_setup_service_not_running(self):
        with patch.object(mc.shutil, "which", return_value="/usr/bin/container"), \
             patch.object(mc, "_service_running", return_value=False):
            rc, out = _cap(mc.setup)
        self.assertEqual(1, rc)
        self.assertIn("system start", out)

    def test_setup_ready_when_the_firewall_is_installed(self):
        with self._ready("ok"):
            rc, out = _cap(mc.setup)
        self.assertEqual(0, rc)
        self.assertIn("ready", out)
        self.install.assert_not_called()

    def test_setup_prints_the_sudo_block_without_root(self):
        for firewall in ("missing", "stale"):
            with self.subTest(firewall=firewall), self._ready(firewall):
                rc, out = _cap(mc.setup)
            self.assertEqual(0, rc)
            self.assertIn(f"sudo tee {mc.host_firewall.RULES_PATH}", out)
            self.assertIn("sudo launchctl bootstrap system", out)
            self.install.assert_not_called()

    def test_setup_says_what_it_changes_before_asking_for_sudo(self):
        with self._ready("missing"):
            _rc, out = _cap(mc.setup)
        self.assertIn("system-wide", out)
        self.assertIn(str(mc.host_firewall.DAEMON_PATH), out)
        self.assertIn("install --undo --backend=macos-container", out)
        self.assertLess(out.index("system-wide"), out.index("sudo tee"))

    def test_setup_moves_agents_off_a_subnet_this_mac_uses(self):
        with self._ready("ok"), patch.object(
                mc.host_firewall, "choose_subnet",
                return_value=("192.168.200.0/24", ["en0 has 192.168.128.7/24"])):
            rc, out = _cap(mc.setup)
        self.assertEqual(0, rc)
        self.assertIn("en0 has 192.168.128.7/24", out)
        self.assertIn("agents move to 192.168.200.0/24", out)
        self.assertIn(f"container network rm {mc.GATEWAY_NETWORK}", out)

    def test_setup_fails_when_every_subnet_is_taken(self):
        with self._ready("missing"), patch.object(
                mc.host_firewall, "choose_subnet", side_effect=RuntimeError("all taken")):
            rc, out = _cap(mc.setup)
        self.assertEqual(1, rc)
        self.assertIn("all taken", out)

    def test_status_reports_a_subnet_clash(self):
        with self._ready("ok"), patch.object(mc.host_firewall, "conflicts",
                                             return_value=["utun4 routes 192.168.0.0/16"]):
            rc, out = _cap(mc.status)
        self.assertEqual(1, rc)
        self.assertIn("utun4 routes 192.168.0.0/16", out)

    def test_setup_as_root_installs_the_firewall_only(self):
        with patch.object(mc.os, "geteuid", return_value=0), \
             patch.object(mc.shutil, "which") as which:
            rc, out = _cap(mc.setup)
        self.assertEqual(0, rc)
        self.install.assert_called_once_with(self.subnet, mc.AGENT_SUBNET_V6)
        which.assert_not_called()  # the container service is per-user
        self.assertIn("Installed the host firewall", out)

    def test_status_all_ok(self):
        with self._ready("ok"):
            rc, out = _cap(mc.status)
        self.assertEqual(0, rc)
        self.assertIn("host firewall: installed", out)

    def test_status_fails_without_the_firewall(self):
        for firewall, text in (("missing", "NOT installed"), ("stale", "out of date")):
            with self.subTest(firewall=firewall), self._ready(firewall):
                rc, out = _cap(mc.status)
            self.assertEqual(1, rc)
            self.assertIn(text, out)
            self.assertIn("install --backend=macos-container", out)

    def test_status_not_macos_no_cli(self):
        with patch.object(mc._container, "is_macos", return_value=False), \
             patch.object(mc.shutil, "which", return_value=None), \
             patch.object(mc.host_firewall, "state", return_value="missing"):
            rc, _ = _cap(mc.status)
        self.assertEqual(1, rc)

    def test_teardown_prints_the_sudo_block_without_root(self):
        rc, out = _cap(mc.teardown)
        self.assertEqual(0, rc)
        self.assertIn(f"sudo launchctl bootout system/{mc.host_firewall.DAEMON_LABEL}", out)
        self.uninstall.assert_not_called()

    def test_teardown_as_root_removes_the_firewall(self):
        with patch.object(mc.os, "geteuid", return_value=0):
            rc, _ = _cap(mc.teardown)
        self.assertEqual(0, rc)
        self.uninstall.assert_called_once_with(self.subnet)

    def test_service_running_false_without_cli(self):
        with patch.object(mc.shutil, "which", return_value=None):
            self.assertFalse(mc._service_running())


# --- netpool renderers (imperative + env overrides) -----------------

class TestNetpoolShellRenderers(unittest.TestCase):
    def test_shell_setup_default(self):
        out = netpool.render_shell_setup(_POOL)
        self.assertIn("firecracker-netpool.sh up", out)
        self.assertNotIn("BOT_BOTTLE_FC_", out)  # no env prefix when all default

    def test_shell_setup_with_overrides(self):
        pool = netpool.from_settings(InfraSettings(fc_ip_base="10.9.0.0", fc_pool_size=4))
        out = netpool.render_shell_setup(pool)
        self.assertIn("BOT_BOTTLE_FC_IP_BASE=10.9.0.0", out)
        self.assertIn("BOT_BOTTLE_FC_POOL_SIZE=4", out)
        self.assertNotIn("BOT_BOTTLE_FC_IFACE_PREFIX", out)


class TestFirecrackerBinaryCheck(unittest.TestCase):
    def test_binary_missing_returns_false(self):
        with patch.object(fc.shutil, "which", return_value=None):
            self.assertFalse(fc._firecracker_binary_ok())

    def test_binary_present_and_runs_ok(self):
        with patch.object(fc.shutil, "which", return_value="/usr/bin/firecracker"), \
             patch.object(fc.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0)):
            self.assertTrue(fc._firecracker_binary_ok())

    def test_binary_found_but_exits_nonzero(self):
        with patch.object(fc.shutil, "which", return_value="/usr/bin/firecracker"), \
             patch.object(fc.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 1)):
            self.assertFalse(fc._firecracker_binary_ok())

    def test_binary_found_but_oserror(self):
        with patch.object(fc.shutil, "which", return_value="/usr/bin/firecracker"), \
             patch.object(fc.subprocess, "run", side_effect=OSError("exec failed")):
            self.assertFalse(fc._firecracker_binary_ok())


class TestFirecrackerKvmCheck(unittest.TestCase):
    def test_kvm_device_absent_returns_false(self):
        with patch.object(fc.os.path, "exists", return_value=False):
            self.assertFalse(fc._kvm_accessible())

    def test_kvm_accessible_when_ioctl_succeeds(self):
        with patch.object(fc.os.path, "exists", return_value=True), \
             patch.object(fc.os, "open", return_value=42), \
             patch.object(fc.os, "close"), \
             patch.object(fc.fcntl, "ioctl", return_value=12):
            self.assertTrue(fc._kvm_accessible())

    def test_kvm_open_rdwr_fails(self):
        with patch.object(fc.os.path, "exists", return_value=True), \
             patch.object(fc.os, "open", side_effect=OSError("Permission denied")):
            self.assertFalse(fc._kvm_accessible())

    def test_kvm_present_but_ioctl_fails(self):
        with patch.object(fc.os.path, "exists", return_value=True), \
             patch.object(fc.os, "open", return_value=42), \
             patch.object(fc.os, "close"), \
             patch.object(fc.fcntl, "ioctl", side_effect=OSError("permission denied")):
            self.assertFalse(fc._kvm_accessible())


class TestFirecrackerArtifactCheck(unittest.TestCase):
    """status() reports missing guest kernel, dropbear binary, and mke2fs."""

    def _apply_all_ok(self, stack: contextlib.ExitStack) -> None:
        """Stub every status() check to pass except what the test overrides."""
        stack.enter_context(patch.object(fc, "_firecracker_binary_ok", return_value=True))
        stack.enter_context(patch.object(fc, "_kvm_accessible", return_value=True))
        k: MagicMock = MagicMock()
        k.is_file.return_value = True
        stack.enter_context(patch.object(fc.util, "kernel_path", return_value=k))
        d: MagicMock = MagicMock()
        d.is_file.return_value = True
        stack.enter_context(patch.object(fc.util, "dropbear_path", return_value=d))
        stack.enter_context(patch.object(fc.shutil, "which", return_value="/usr/bin/x"))
        stack.enter_context(patch.object(netpool.NetPool, "missing_taps", return_value=[]))
        stack.enter_context(patch.object(netpool.NetPool, "overlapping_routes", return_value=[]))
        stack.enter_context(patch.object(fc, "_report_persistence", lambda: None))

    def test_status_fails_when_kernel_missing(self):
        with contextlib.ExitStack() as stack:
            self._apply_all_ok(stack)
            k: MagicMock = MagicMock()
            k.is_file.return_value = False
            stack.enter_context(patch.object(fc.util, "kernel_path", return_value=k))
            rc, out = _cap(lambda: fc.status(_S))
        self.assertEqual(1, rc)
        self.assertIn("guest kernel: NOT found", out)

    def test_status_fails_when_dropbear_missing(self):
        with contextlib.ExitStack() as stack:
            self._apply_all_ok(stack)
            d: MagicMock = MagicMock()
            d.is_file.return_value = False
            stack.enter_context(patch.object(fc.util, "dropbear_path", return_value=d))
            rc, out = _cap(lambda: fc.status(_S))
        self.assertEqual(1, rc)
        self.assertIn("dropbear: NOT found", out)

    def test_status_fails_when_mke2fs_missing(self):
        with contextlib.ExitStack() as stack:
            self._apply_all_ok(stack)
            stack.enter_context(patch.object(
                fc.shutil, "which",
                side_effect=lambda cmd: (None if cmd == "mke2fs" else "/usr/bin/x"),  # type: ignore[misc]
            ))
            rc, out = _cap(lambda: fc.status(_S))
        self.assertEqual(1, rc)
        self.assertIn("mke2fs: NOT found", out)


class TestFirecrackerStatusRuntime(unittest.TestCase):
    """status() reports binary and KVM problems and returns non-zero."""

    def _apply_pool_ok(self, stack: contextlib.ExitStack) -> None:
        kernel_mock = MagicMock()
        kernel_mock.is_file.return_value = True
        dropbear_mock = MagicMock()
        dropbear_mock.is_file.return_value = True
        stack.enter_context(patch.object(fc.util, "kernel_path", return_value=kernel_mock))
        stack.enter_context(patch.object(fc.util, "dropbear_path", return_value=dropbear_mock))
        stack.enter_context(patch.object(netpool.NetPool, "missing_taps", return_value=[]))
        stack.enter_context(patch.object(netpool.NetPool, "overlapping_routes", return_value=[]))
        stack.enter_context(patch.object(fc, "_report_persistence", lambda: None))

    def test_status_fails_when_binary_missing(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(fc, "_firecracker_binary_ok", return_value=False))
            stack.enter_context(
                patch.object(fc, "_kvm_accessible", return_value=True))
            stack.enter_context(
                patch.object(fc.shutil, "which", return_value=None))
            self._apply_pool_ok(stack)
            rc, out = _cap(lambda: fc.status(_S))
        self.assertEqual(1, rc)
        self.assertIn("NOT found on PATH", out)

    def test_status_fails_when_kvm_not_accessible(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(fc, "_firecracker_binary_ok", return_value=True))
            stack.enter_context(
                patch.object(fc.shutil, "which", return_value="/usr/bin/firecracker"))
            stack.enter_context(
                patch.object(fc, "_kvm_accessible", return_value=False))
            stack.enter_context(
                patch.object(fc.os.path, "exists", return_value=True))
            self._apply_pool_ok(stack)
            rc, out = _cap(lambda: fc.status(_S))
        self.assertEqual(1, rc)
        self.assertIn("not accessible", out)

    def test_status_ok_when_binary_and_kvm_ready(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(fc, "_firecracker_binary_ok", return_value=True))
            stack.enter_context(
                patch.object(fc.shutil, "which", return_value="/usr/bin/firecracker"))
            stack.enter_context(
                patch.object(fc, "_kvm_accessible", return_value=True))
            stack.enter_context(
                patch.object(netpool.NetPool, "nft_table_present", return_value=True))
            self._apply_pool_ok(stack)
            rc, out = _cap(lambda: fc.status(_S))
        self.assertEqual(0, rc)
        self.assertIn("firecracker binary: ok", out)
        self.assertIn("KVM:", out)


class TestBackendStatusQuiet(unittest.TestCase):
    """status(quiet=True) routes the underlying status() call through
    redirect_stderr so diagnostic output is suppressed. Verify the return
    code is propagated and that calling with quiet=False leaves the non-quiet
    path active (covered by the other TestDockerSetupStatus tests)."""

    def test_docker_quiet_true_propagates_return_code(self):
        from bot_bottle.backend.docker.backend import DockerBottleBackend
        with patch.object(dk, "status", return_value=0):
            self.assertEqual(0, DockerBottleBackend(_S).status(quiet=True))

    def test_docker_quiet_true_suppresses_stderr(self):
        import io as _io
        from bot_bottle.backend.docker.backend import DockerBottleBackend

        def _loud_status():
            import sys
            print("should be suppressed", file=sys.stderr)
            return 0

        with patch.object(dk, "status", side_effect=_loud_status):
            buf = _io.StringIO()
            with contextlib.redirect_stderr(buf):
                DockerBottleBackend(_S).status(quiet=True)
        self.assertEqual("", buf.getvalue())

    def test_firecracker_quiet_true_propagates_return_code(self):
        from bot_bottle.backend.firecracker.backend import FirecrackerBottleBackend
        with patch.object(fc, "status", return_value=1):
            self.assertEqual(1, FirecrackerBottleBackend(_S).status(quiet=True))

    def test_macos_quiet_true_propagates_return_code(self):
        from bot_bottle.backend.macos_container.backend import MacosContainerBottleBackend
        with patch.object(mc, "status", return_value=0):
            self.assertEqual(0, MacosContainerBottleBackend(_S).status(quiet=True))


if __name__ == "__main__":
    unittest.main()
