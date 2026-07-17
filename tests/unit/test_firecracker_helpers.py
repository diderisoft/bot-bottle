"""Unit: small Firecracker helpers — guest-IP parsing, the unprivileged
nft/TAP probes, console-tail, and the require_firecracker preflight
branches. Mock subprocess/os so nothing needs KVM or a live VM.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
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

    def test_orch_slot_is_top_of_ip_base_16(self):
        # Dedicated orchestrator link: /31 at the top of the IP_BASE /16,
        # clear of the pool (bottom of the block). Must match the shell
        # script's orch_host()/orch_guest() and be a non-pool index.
        with patch.dict("os.environ", {"BOT_BOTTLE_FC_IP_BASE": "10.243.0.0"}):
            s = netpool.orch_slot()
        self.assertEqual(netpool.ORCH_IFACE, s.iface)
        self.assertEqual("10.243.255.0", s.host_ip)
        self.assertEqual("10.243.255.1", s.guest_ip)
        self.assertEqual(-1, s.index)
        # Never collides with a pool slot's guest address.
        with patch.dict("os.environ", {"BOT_BOTTLE_FC_IP_BASE": "10.243.0.0"}):
            pool_guests = {sl.guest_ip for sl in netpool.all_slots()}
        self.assertNotIn(s.guest_ip, pool_guests)


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


class TestBuildCommittedRootfsDir(unittest.TestCase):
    """Resume prepares the base rootfs dir from the freezer's snapshot tar
    with no Docker: extract, recreate the excluded mount points, inject the
    guest boot bits."""

    def _make_tar(self, tmp: Path) -> Path:
        import tarfile

        src = tmp / "src"
        (src / "home" / "node").mkdir(parents=True)
        (src / "home" / "node" / "hello").write_text("hi")
        tar_path = tmp / "rootfs.tar"
        with tarfile.open(tar_path, "w") as tar:
            tar.add(src, arcname=".")
        return tar_path

    def test_extracts_recreates_mountpoints_and_injects_boot(self):
        with tempfile.TemporaryDirectory(prefix="fc-committed.") as d:
            tmp = Path(d)
            tar_path = self._make_tar(tmp)
            # Stand in for the static dropbear that inject_guest_boot copies.
            dropbear = tmp / "dropbear"
            dropbear.write_text("#!/bin/true\n")
            cache = tmp / "cache"
            cache.mkdir()

            with patch.object(util, "cache_dir", return_value=cache), \
                 patch.object(util, "dropbear_path", return_value=dropbear), \
                 patch.object(util, "info"):
                base = util.build_committed_rootfs_dir(tar_path)

            self.assertEqual("hi", (base / "home" / "node" / "hello").read_text())
            for mount_point in ("proc", "sys", "dev", "run"):
                self.assertTrue((base / mount_point).is_dir(),
                                f"missing recreated mount point /{mount_point}")
            self.assertTrue((base / "bb-dropbear").is_file())
            self.assertTrue((base / "bb-init").is_file())
            self.assertTrue((base / ".bb-ready").is_file())

    def test_caches_on_repeat_and_reextracts_after_refreeze(self):
        with tempfile.TemporaryDirectory(prefix="fc-committed.") as d:
            tmp = Path(d)
            tar_path = self._make_tar(tmp)
            dropbear = tmp / "dropbear"
            dropbear.write_text("#!/bin/true\n")
            cache = tmp / "cache"
            cache.mkdir()

            ctx = [
                patch.object(util, "cache_dir", return_value=cache),
                patch.object(util, "dropbear_path", return_value=dropbear),
                patch.object(util, "info"),
            ]
            for c in ctx:
                c.start()
            self.addCleanup(lambda: [c.stop() for c in ctx])

            real_run = subprocess.run
            calls = {"n": 0}

            def counting_run(argv: list[str], *a: Any, **k: Any) -> Any:
                if argv and argv[0] == "tar":
                    calls["n"] += 1
                return real_run(argv, *a, **k)

            with patch.object(util.subprocess, "run", side_effect=counting_run):
                first = util.build_committed_rootfs_dir(tar_path)
                second = util.build_committed_rootfs_dir(tar_path)
                self.assertEqual(first, second)
                self.assertEqual(1, calls["n"])  # cached — no re-extract

                # A re-freeze rewrites the tar; a new size/mtime -> new cache
                # key -> re-extract. Force a distinct mtime so the test isn't
                # at the mercy of filesystem timestamp granularity.
                import tarfile
                extra = tmp / "extra"
                extra.mkdir()
                (extra / "note").write_text("v2")
                with tarfile.open(tar_path, "w") as tar:
                    tar.add(extra, arcname=".")
                st = tar_path.stat()
                os.utime(tar_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

                third = util.build_committed_rootfs_dir(tar_path)
                self.assertNotEqual(first, third)
                self.assertEqual(2, calls["n"])


class TestInjectGuestBootSymlinkSafe(unittest.TestCase):
    """A committed snapshot is guest-controlled: inject_guest_boot must not
    follow a planted symlink and overwrite a host file during resume."""

    def test_planted_symlink_does_not_escape_staging_tree(self):
        with tempfile.TemporaryDirectory(prefix="fc-inject.") as d:
            tmp = Path(d)
            dropbear = tmp / "dropbear"
            dropbear.write_bytes(b"DROPBEAR")
            # A host file the malicious snapshot tries to clobber.
            victim = tmp / "victim"
            victim.write_text("original")

            rootfs = tmp / "rootfs"
            rootfs.mkdir()
            # The snapshot planted bb-init/bb-dropbear as symlinks to it.
            (rootfs / "bb-init").symlink_to(victim)
            (rootfs / "bb-dropbear").symlink_to(victim)

            with patch.object(util, "dropbear_path", return_value=dropbear):
                util.inject_guest_boot(rootfs, init_script="#!/bin/sh\nreal\n")

            # Host file untouched; the staged paths are fresh regular files.
            self.assertEqual("original", victim.read_text())
            self.assertFalse((rootfs / "bb-init").is_symlink())
            self.assertFalse((rootfs / "bb-dropbear").is_symlink())
            self.assertEqual("#!/bin/sh\nreal\n", (rootfs / "bb-init").read_text())
            self.assertEqual(b"DROPBEAR", (rootfs / "bb-dropbear").read_bytes())


class TestCommitRootfsPermissions(unittest.TestCase):
    """The snapshot tar can hold the bottle's private workspace, so the
    freezer must write it owner-only (0600)."""

    def test_snapshot_created_owner_only(self):
        with tempfile.TemporaryDirectory(prefix="fc-freeze.") as d:
            tmp = Path(d)
            key = tmp / "key"
            key.write_text("K")
            tar_path = tmp / "state" / "committed-rootfs.tar"

            def fake_run(argv: list[str], *a: Any, **k: Any) -> Any:
                # Emulate the ssh|tar pipe streaming the snapshot to stdout.
                k["stdout"].write(b"TARDATA")
                return subprocess.CompletedProcess(argv, 0, b"", b"")

            with patch.object(freezer.util, "ssh_base_argv", return_value=["ssh"]), \
                 patch.object(freezer.subprocess, "run", side_effect=fake_run):
                freezer._commit_rootfs_via_ssh(key, "10.0.0.1", tar_path)

            self.assertEqual(b"TARDATA", tar_path.read_bytes())
            self.assertEqual(0o600, stat.S_IMODE(tar_path.stat().st_mode))


if __name__ == "__main__":
    unittest.main()
