"""Unit: small Firecracker helpers — guest-IP parsing, the unprivileged
nft/TAP probes, console-tail, and the require_firecracker preflight
branches. Mock subprocess/os so nothing needs KVM or a live VM.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.backend.firecracker import firecracker_vm, freezer, netpool, util


class TestGuestIpFromConfig(unittest.TestCase):
    def _write(self, obj: object) -> Path:
        d = tempfile.mkdtemp(prefix="fc-cfg.")
        p = Path(d) / "config.json"
        p.write_text(json.dumps(obj))
        self.addCleanup(lambda: p.unlink(missing_ok=True))
        return p

    def test_extracts_guest_ip(self):
        cfg = {"boot-source": {"boot_args":
               "console=ttyS0 ip=10.243.0.1::10.243.0.0:255.255.255.254::eth0:off"}}
        self.assertEqual("10.243.0.1", freezer._guest_ip_from_config(self._write(cfg)))

    def test_no_ip_token(self):
        self.assertEqual("", freezer._guest_ip_from_config(
            self._write({"boot-source": {"boot_args": "console=ttyS0"}})))

    def test_missing_file(self):
        self.assertEqual("", freezer._guest_ip_from_config(Path("/nope/config.json")))

    def test_malformed_json(self):
        d = tempfile.mkdtemp(prefix="fc-cfg.")
        p = Path(d) / "config.json"
        p.write_text("{not json")
        self.addCleanup(lambda: p.unlink(missing_ok=True))
        self.assertEqual("", freezer._guest_ip_from_config(p))


class TestNetpoolProbes(unittest.TestCase):
    def test_run_ok_true_on_zero(self):
        with patch.object(netpool.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0)):
            self.assertTrue(netpool._run_ok(["true"]))

    def test_run_ok_false_on_missing_binary(self):
        with patch.object(netpool.subprocess, "run", side_effect=FileNotFoundError):
            self.assertFalse(netpool._run_ok(["nft"]))

    def test_tap_and_nft_probes(self):
        with patch.object(netpool, "_run_ok", return_value=True) as ok:
            self.assertTrue(netpool.tap_present("bbfc0"))
            self.assertTrue(netpool.nft_table_present())
        self.assertEqual(2, ok.call_count)

    def test_missing_taps(self):
        with patch.dict("os.environ", {"BOT_BOTTLE_FC_POOL_SIZE": "2"}), \
             patch.object(netpool, "tap_present", side_effect=[True, False]):
            self.assertEqual(["bbfc1"], netpool.missing_taps())


class TestConsoleTail(unittest.TestCase):
    def test_reads_tail(self):
        d = tempfile.mkdtemp(prefix="fc-con.")
        p = Path(d) / "console.log"
        p.write_text("l1\nl2\nl3\n")
        self.addCleanup(lambda: p.unlink(missing_ok=True))
        out = firecracker_vm._console_tail(p, lines=2)
        self.assertIn("l2", out)
        self.assertIn("l3", out)
        self.assertNotIn("l1", out)

    def test_missing_file(self):
        self.assertIn("no console log",
                      firecracker_vm._console_tail(Path("/nope/console.log")))


class TestRequireFirecracker(unittest.TestCase):
    def _die(self):
        return patch.object(util, "die", side_effect=SystemExit("die"))

    def test_dies_when_not_linux(self):
        with patch.object(util, "is_linux", return_value=False), self._die():
            with self.assertRaises(SystemExit):
                util.require_firecracker()

    def test_dies_when_binary_missing(self):
        with patch.object(util, "is_linux", return_value=True), \
             patch.object(util.shutil, "which", return_value=None), \
             patch.object(util, "info"), self._die():
            with self.assertRaises(SystemExit):
                util.require_firecracker()

    def test_dies_when_kvm_missing(self):
        with patch.object(util, "is_linux", return_value=True), \
             patch.object(util.shutil, "which", return_value="/usr/bin/firecracker"), \
             patch.object(util.os.path, "exists", return_value=False), self._die():
            with self.assertRaises(SystemExit):
                util.require_firecracker()

    def test_dies_when_kernel_missing(self):
        with patch.object(util, "is_linux", return_value=True), \
             patch.object(util.shutil, "which", return_value="/usr/bin/firecracker"), \
             patch.object(util, "_require_kvm", lambda: None), \
             patch.object(util.Path, "is_file", return_value=False), self._die():
            with self.assertRaises(SystemExit):
                util.require_firecracker()


if __name__ == "__main__":
    unittest.main()
