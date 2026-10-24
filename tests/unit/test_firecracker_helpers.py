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
from bot_bottle.contract.hostctl import InfraSettings

_INFRA = InfraSettings()
_POOL = netpool.from_settings(_INFRA)


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
        self.assertEqual("10.243.0.1", util.guest_ip_from_config(self._write(cfg)))

    def test_no_ip_token(self):
        self.assertEqual("", util.guest_ip_from_config(
            self._write({"boot-source": {"boot_args": "console=ttyS0"}})))

    def test_missing_file(self):
        self.assertEqual("", util.guest_ip_from_config(Path("/nope/config.json")))

    def test_malformed_json(self):
        d = tempfile.mkdtemp(prefix="fc-cfg.")
        p = Path(d) / "config.json"
        p.write_text("{not json")
        self.addCleanup(lambda: p.unlink(missing_ok=True))
        self.assertEqual("", util.guest_ip_from_config(p))


class TestNetpoolProbes(unittest.TestCase):
    def test_run_ok_true_on_zero(self):
        with patch.object(netpool.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0)):
            self.assertTrue(netpool._run_ok(["true"]))

    def test_run_ok_false_on_missing_binary(self):
        with patch.object(netpool.subprocess, "run", side_effect=FileNotFoundError):
            self.assertFalse(netpool._run_ok(["nft"]))

    def test_run_ok_false_on_unexecutable_binary(self):
        # A name on PATH that this user can't execute raises PermissionError,
        # not FileNotFoundError — CPython reports that EACCES in preference to
        # the ENOENT from the other PATH entries. Catching only the latter made
        # `doctor` die with a traceback on a fresh macOS account.
        with patch.object(netpool.subprocess, "run",
                          side_effect=PermissionError(13, "Permission denied", "ip")):
            self.assertFalse(netpool._run_ok(["ip", "link", "show", "bbfc0"]))

    def test_overlapping_routes_empty_when_ip_unusable(self):
        for exc in (FileNotFoundError, PermissionError(13, "Permission denied", "ip")):
            with self.subTest(exc=type(exc).__name__), \
                 patch.object(netpool.subprocess, "run", side_effect=exc):
                self.assertEqual([], _POOL.overlapping_routes())

    def test_tap_and_nft_probes(self):
        with patch.object(netpool, "_run_ok", return_value=True) as ok:
            self.assertTrue(netpool.tap_present("bbfc0"))
            self.assertTrue(_POOL.nft_table_present())
        self.assertEqual(2, ok.call_count)

    def test_missing_taps(self):
        pool = netpool.from_settings(InfraSettings(fc_pool_size=2))
        with patch.object(netpool, "tap_present", side_effect=[True, False]):
            self.assertEqual([pool.slot(1).iface], pool.missing_taps())

    def test_orch_slot_is_top_of_ip_base_16(self):
        # Dedicated orchestrator link: /31 at the top of the IP_BASE /16,
        # clear of the pool (bottom of the block). Must match the shell
        # script's orch_host()/orch_guest() and be a non-pool index.
        pool = netpool.from_settings(InfraSettings(fc_ip_base="10.243.0.0"))
        s = pool.orch_slot()
        self.assertEqual(pool.orch_iface, s.iface)
        self.assertEqual("10.243.255.0", s.host_ip)
        self.assertEqual("10.243.255.1", s.guest_ip)
        self.assertEqual(-1, s.index)
        # Never collides with a pool slot's guest address.
        pool_guests = {sl.guest_ip for sl in pool.all_slots()}
        self.assertNotIn(s.guest_ip, pool_guests)

    def test_gw_slot_is_second_link_above_orch(self):
        # Dedicated gateway (data-plane) link: the /31 immediately above the
        # orchestrator link. Must match the shell script's gw_host()/gw_guest()
        # and be a distinct non-pool index that never collides with the
        # orchestrator link or the agent pool.
        pool = netpool.from_settings(InfraSettings(fc_ip_base="10.243.0.0"))
        s = pool.gw_slot()
        orch = pool.orch_slot()
        pool_guests = {sl.guest_ip for sl in pool.all_slots()}
        self.assertEqual(pool.gw_iface, s.iface)
        self.assertEqual("10.243.255.2", s.host_ip)
        self.assertEqual("10.243.255.3", s.guest_ip)
        self.assertEqual(-2, s.index)
        self.assertNotEqual(orch.guest_ip, s.guest_ip)  # distinct from the orch link
        self.assertNotIn(s.guest_ip, pool_guests)       # distinct from the pool


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
                util.require_firecracker(_INFRA)

    def test_dies_when_binary_missing(self):
        with patch.object(util, "is_linux", return_value=True), \
             patch.object(util.shutil, "which", return_value=None), \
             patch.object(util, "info"), self._die():
            with self.assertRaises(SystemExit):
                util.require_firecracker(_INFRA)

    def test_dies_when_kvm_missing(self):
        with patch.object(util, "is_linux", return_value=True), \
             patch.object(util.shutil, "which", return_value="/usr/bin/firecracker"), \
             patch.object(util.os.path, "exists", return_value=False), self._die():
            with self.assertRaises(SystemExit):
                util.require_firecracker(_INFRA)

    def test_dies_when_kernel_missing(self):
        with patch.object(util, "is_linux", return_value=True), \
             patch.object(util.shutil, "which", return_value="/usr/bin/firecracker"), \
             patch.object(util, "_require_kvm", lambda: None), \
             patch.object(util.Path, "is_file", return_value=False), self._die():
            with self.assertRaises(SystemExit):
                util.require_firecracker(_INFRA)


class TestBuildBaseRootfsDirHit(unittest.TestCase):
    def test_a_hit_refreshes_the_ready_marker_and_exports_nothing(self):
        # Pruning goes by age, so the marker must say when it was last used.
        with tempfile.TemporaryDirectory(prefix="fc-base.") as d:
            cache = Path(d)
            base = cache / "rootfs" / "abc123-gw"
            base.mkdir(parents=True)
            ready = base / ".bb-ready"
            ready.write_text("ok\n")
            os.utime(ready, (1_000_000, 1_000_000))
            with patch.object(util, "cache_dir", return_value=cache), \
                 patch.object(util, "docker_image_id", return_value="abc123"), \
                 patch.object(util, "export_image_rootfs") as export:
                got = util.build_base_rootfs_dir("img", _INFRA, variant="-gw")
            self.assertEqual(base, got)
            export.assert_not_called()
            self.assertGreater(ready.stat().st_mtime, 1_000_000)


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
                base = util.build_committed_rootfs_dir(tar_path, _INFRA)

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
                first = util.build_committed_rootfs_dir(tar_path, _INFRA)
                second = util.build_committed_rootfs_dir(tar_path, _INFRA)
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

                third = util.build_committed_rootfs_dir(tar_path, _INFRA)
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
                util.inject_guest_boot(rootfs, _INFRA, init_script="#!/bin/sh\nreal\n")

            # Host file untouched; the staged paths are fresh regular files.
            self.assertEqual("original", victim.read_text())
            self.assertFalse((rootfs / "bb-init").is_symlink())
            self.assertFalse((rootfs / "bb-dropbear").is_symlink())
            self.assertEqual("#!/bin/sh\nreal\n", (rootfs / "bb-init").read_text())
            self.assertEqual(b"DROPBEAR", (rootfs / "bb-dropbear").read_bytes())


class TestCommitRootfsPermissions(unittest.TestCase):
    """The snapshot tar can hold the bottle's private workspace, so the
    freezer must write it owner-only (0600)."""

    def _commit(self, tar_path: Path) -> int:
        """Run _commit_rootfs_via_ssh with a stubbed ssh|tar pipe; return the
        mode of the open partial observed mid-stream (from subprocess.run)."""
        key = tar_path.parent.parent / "key"
        key.write_text("K")
        seen: dict[str, int] = {}

        def fake_run(argv: list[str], *a: Any, **k: Any) -> Any:
            out = k["stdout"]
            seen["mode"] = stat.S_IMODE(os.fstat(out.fileno()).st_mode)
            out.write(b"TARDATA")
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with patch.object(freezer.util, "ssh_base_argv", return_value=["ssh"]), \
             patch.object(freezer.subprocess, "run", side_effect=fake_run):
            freezer._commit_rootfs_via_ssh(key, "10.0.0.1", tar_path)
        return seen["mode"]

    def test_snapshot_created_owner_only(self):
        with tempfile.TemporaryDirectory(prefix="fc-freeze.") as d:
            tar_path = Path(d) / "state" / "committed-rootfs.tar"
            tar_path.parent.mkdir()
            stream_mode = self._commit(tar_path)

            self.assertEqual(0o600, stream_mode)  # private during the stream
            self.assertEqual(b"TARDATA", tar_path.read_bytes())
            self.assertEqual(0o600, stat.S_IMODE(tar_path.stat().st_mode))

    def test_leftover_world_readable_partial_is_recreated_private(self):
        """A partial left 0644 by an interrupted prior run must not keep the
        new snapshot world-readable while it streams."""
        with tempfile.TemporaryDirectory(prefix="fc-freeze.") as d:
            tar_path = Path(d) / "state" / "committed-rootfs.tar"
            tar_path.parent.mkdir()
            partial = tar_path.with_name(tar_path.name + ".partial")
            partial.write_bytes(b"stale")
            os.chmod(partial, 0o644)

            stream_mode = self._commit(tar_path)

            self.assertEqual(0o600, stream_mode)
            self.assertEqual(b"TARDATA", tar_path.read_bytes())
            self.assertEqual(0o600, stat.S_IMODE(tar_path.stat().st_mode))


if __name__ == "__main__":
    unittest.main()
