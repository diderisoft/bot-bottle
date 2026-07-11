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

from bot_bottle.backend.docker import setup as dk
from bot_bottle.backend.firecracker import netpool
from bot_bottle.backend.firecracker import setup as fc
from bot_bottle.backend.macos_container import setup as mc


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
            patch.object(fc, "_warn_overlaps", lambda: None),
        ]
        for p in self._p:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._p])

    def test_nixos_prints_module_import(self):
        with patch.object(fc, "_is_nixos", return_value=True):
            rc, out = _cap(fc.setup)
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
            rc, out = _cap(fc.setup)
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
            rc, out = _cap(fc.setup)
        self.assertEqual(0, rc)
        self.assertIn("enable --now` failed", out)

    def test_systemd_nonroot_prints_block(self):
        with patch.object(fc, "_is_nixos", return_value=False), \
             patch.object(fc, "_has_systemd", return_value=True), \
             patch.object(fc.os, "geteuid", return_value=1000):
            rc, out = _cap(fc.setup)
        self.assertEqual(0, rc)
        self.assertIn("sudo tee", out)
        self.assertIn("systemctl enable --now", out)

    def test_no_systemd_prints_shell(self):
        with patch.object(fc, "_is_nixos", return_value=False), \
             patch.object(fc, "_has_systemd", return_value=False):
            rc, out = _cap(fc.setup)
        self.assertEqual(0, rc)
        self.assertIn("firecracker-netpool.sh up", out)


class TestFirecrackerWarnOverlaps(unittest.TestCase):
    def test_emits_on_conflict(self):
        conflict = netpool.RouteConflict(dst="10.243.0.0/24", dev="eth0")
        with patch.object(fc.netpool, "overlapping_routes", return_value=[conflict]):
            _, out = _cap(fc._warn_overlaps)
        self.assertIn("overlaps existing routes", out)

    def test_silent_when_clear(self):
        with patch.object(fc.netpool, "overlapping_routes", return_value=[]):
            _, out = _cap(fc._warn_overlaps)
        self.assertEqual("", out)


class TestFirecrackerTeardown(unittest.TestCase):
    def test_nixos(self):
        with patch.object(fc, "_is_nixos", return_value=True):
            rc, out = _cap(fc.teardown)
        self.assertEqual(0, rc)
        self.assertIn("enable = false", out)

    def test_systemd_root_removes_unit(self):
        unit = MagicMock()
        with patch.object(fc, "_is_nixos", return_value=False), \
             patch.object(fc, "_has_systemd", return_value=True), \
             patch.object(fc.os, "geteuid", return_value=0), \
             patch.object(fc, "_UNIT_PATH", unit), \
             patch.object(fc.subprocess, "run", return_value=_ok()):
            rc, out = _cap(fc.teardown)
        self.assertEqual(0, rc)
        unit.unlink.assert_called_once()
        self.assertIn("removed", out)

    def test_systemd_nonroot_prints(self):
        with patch.object(fc, "_is_nixos", return_value=False), \
             patch.object(fc, "_has_systemd", return_value=True), \
             patch.object(fc.os, "geteuid", return_value=1000):
            rc, out = _cap(fc.teardown)
        self.assertEqual(0, rc)
        self.assertIn("systemctl disable", out)

    def test_no_systemd(self):
        with patch.object(fc, "_is_nixos", return_value=False), \
             patch.object(fc, "_has_systemd", return_value=False):
            rc, out = _cap(fc.teardown)
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


# --- macos-container ------------------------------------------------

class TestMacosSetupStatus(unittest.TestCase):
    def test_setup_not_macos(self):
        with patch.object(mc._container, "is_macos", return_value=False):
            rc, out = _cap(mc.setup)
        self.assertEqual(1, rc)
        self.assertIn("requires macOS", out)

    def test_setup_no_cli(self):
        with patch.object(mc._container, "is_macos", return_value=True), \
             patch.object(mc.shutil, "which", return_value=None):
            rc, out = _cap(mc.setup)
        self.assertEqual(1, rc)
        self.assertIn("not found on PATH", out)

    def test_setup_service_not_running(self):
        with patch.object(mc._container, "is_macos", return_value=True), \
             patch.object(mc.shutil, "which", return_value="/usr/bin/container"), \
             patch.object(mc, "_service_running", return_value=False):
            rc, out = _cap(mc.setup)
        self.assertEqual(1, rc)
        self.assertIn("system start", out)

    def test_setup_ready(self):
        with patch.object(mc._container, "is_macos", return_value=True), \
             patch.object(mc.shutil, "which", return_value="/usr/bin/container"), \
             patch.object(mc, "_service_running", return_value=True):
            rc, out = _cap(mc.setup)
        self.assertEqual(0, rc)
        self.assertIn("ready", out)

    def test_status_all_ok(self):
        with patch.object(mc._container, "is_macos", return_value=True), \
             patch.object(mc.shutil, "which", return_value="/usr/bin/container"), \
             patch.object(mc, "_service_running", return_value=True):
            rc, _ = _cap(mc.status)
        self.assertEqual(0, rc)

    def test_status_not_macos_no_cli(self):
        with patch.object(mc._container, "is_macos", return_value=False), \
             patch.object(mc.shutil, "which", return_value=None):
            rc, _ = _cap(mc.status)
        self.assertEqual(1, rc)

    def test_teardown_noop(self):
        rc, out = _cap(mc.teardown)
        self.assertEqual(0, rc)
        self.assertIn("nothing to undo", out)

    def test_service_running_false_without_cli(self):
        with patch.object(mc.shutil, "which", return_value=None):
            self.assertFalse(mc._service_running())


# --- netpool renderers (imperative + env overrides) -----------------

class TestNetpoolShellRenderers(unittest.TestCase):
    def test_shell_setup_default(self):
        with patch.dict("os.environ", {}, clear=True):
            out = netpool.render_shell_setup()
        self.assertIn("firecracker-netpool.sh up", out)
        self.assertNotIn("BOT_BOTTLE_FC_", out)  # no env prefix when all default

    def test_shell_setup_with_overrides(self):
        # pool_size()/ip_base() re-read the env; IFACE_PREFIX is a
        # module constant, so exercise the function-backed knobs.
        with patch.dict("os.environ", {"BOT_BOTTLE_FC_IP_BASE": "10.9.0.0",
                                       "BOT_BOTTLE_FC_POOL_SIZE": "4"}):
            out = netpool.render_shell_setup()
        self.assertIn("BOT_BOTTLE_FC_IP_BASE=10.9.0.0", out)
        self.assertIn("BOT_BOTTLE_FC_POOL_SIZE=4", out)


if __name__ == "__main__":
    unittest.main()
