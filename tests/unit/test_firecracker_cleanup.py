"""Unit: Firecracker cleanup (orphan enumeration + removal) and the
thin backend classmethod delegates.

Cleanup shells out to pgrep/docker and touches the run dir; mock those so
the orphan-discovery + removal logic is covered without a live host. The
delegate tests just assert each backend's setup/status/teardown/cleanup
classmethods forward to their module.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.backend import EnumerationError
from bot_bottle.backend.firecracker import cleanup as fc_cleanup
from bot_bottle.backend.firecracker.bottle_cleanup_plan import (
    FirecrackerBottleCleanupPlan,
)


def _proc(stdout: str = "", returncode: int = 0) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class TestProcessScan(unittest.TestCase):
    def test_run_dir_of_matches_only_direct_children(self):
        run_root = Path("/cache/run")
        self.assertEqual(
            Path("/cache/run/dev-a"),
            fc_cleanup._run_dir_of(
                ("firecracker", "--config-file",
                 f"{run_root}/dev-a/config.json"), run_root
            ),
        )
        # infra/builder VMs elsewhere, or nested paths, are not ours.
        self.assertIsNone(
            fc_cleanup._run_dir_of(
                ("firecracker", "--config-file", "/elsewhere/config.json"),
                run_root,
            )
        )
        self.assertIsNone(
            fc_cleanup._run_dir_of(("firecracker", "--no-config"), run_root)
        )

    def test_scan_splits_live_dirs_from_orphan_pids(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            (run_root / "live-a").mkdir()  # dir present -> live VM, protected
            # "gone-b" dir intentionally absent -> lingering VMM, orphan pid
            args = {
                111: ("firecracker", "--config-file",
                      f"{run_root}/live-a/config.json"),
                222: ("firecracker", "--config-file",
                      f"{run_root}/gone-b/config.json"),
                333: ("firecracker", "--config-file", "/elsewhere/config.json"),
            }
            with patch.object(
                fc_cleanup.subprocess, "run",
                return_value=_proc("111\n222\n333\nnotanint\n"),
            ), patch.object(
                fc_cleanup, "_process_args", side_effect=args.get,
            ):
                live, orphan_pids = fc_cleanup._scan_processes(run_root)
        self.assertEqual({str(run_root / "live-a")}, live)
        self.assertEqual([222], orphan_pids)

    def test_scan_preserves_spaces_in_config_path(self):
        with tempfile.TemporaryDirectory(prefix="fc cache ") as tmp:
            run_root = Path(tmp)
            live = run_root / "live bottle"
            live.mkdir()
            with patch.object(
                fc_cleanup.subprocess, "run", return_value=_proc("111\n"),
            ), patch.object(
                fc_cleanup, "_process_args",
                return_value=("firecracker", "--config-file",
                              str(live / "config.json")),
            ):
                self.assertEqual(
                    ({str(live)}, []),
                    fc_cleanup._scan_processes(run_root),
                )

    def test_scan_empty_when_pgrep_finds_no_processes(self):
        with patch.object(fc_cleanup.subprocess, "run", return_value=_proc(returncode=1)):
            self.assertEqual((set(), []), fc_cleanup._scan_processes(Path("/x")))

    def test_scan_raises_when_pgrep_errors(self):
        proc = subprocess.CompletedProcess(
            [], 2, stdout="", stderr="invalid process expression",
        )
        with (
            patch.object(fc_cleanup.subprocess, "run", return_value=proc),
            self.assertRaisesRegex(EnumerationError, "invalid process expression"),
        ):
            fc_cleanup._scan_processes(Path("/x"))

    def test_prepare_cleanup_does_not_plan_deletions_when_scan_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            live = run_root / "live-a"
            live.mkdir()
            with (
                patch.object(fc_cleanup, "_run_root", return_value=run_root),
                patch.object(
                    fc_cleanup.subprocess,
                    "run",
                    return_value=_proc(returncode=2),
                ),
                self.assertRaises(EnumerationError),
            ):
                fc_cleanup.prepare_cleanup()
            self.assertTrue(live.is_dir())

    def test_live_run_dirs_returns_paths_in_stable_order(self):
        with patch.object(fc_cleanup, "_run_root", return_value=Path("/run")), \
             patch.object(fc_cleanup, "_scan_processes",
                          return_value=({"/run/b", "/run/a"}, [])):
            self.assertEqual(
                (Path("/run/a"), Path("/run/b")), fc_cleanup.live_run_dirs(),
            )

    def test_orphan_run_dirs_excludes_live_and_missing_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            (run_root / "live-a").mkdir()
            (run_root / "dead-b").mkdir()
            live = {str(run_root / "live-a")}
            self.assertEqual(
                [str(run_root / "dead-b")],
                fc_cleanup._orphan_run_dirs(run_root, live),
            )
        # absent run root -> nothing to reap
        self.assertEqual([], fc_cleanup._orphan_run_dirs(Path("/nope/run"), set()))

    def test_prepare_cleanup_reaps_orphans_only(self):
        """The live VM's dir is never in the plan; the dead one is."""
        with tempfile.TemporaryDirectory() as tmp:
            run_root = Path(tmp)
            (run_root / "live-a").mkdir()
            (run_root / "dead-b").mkdir()
            with patch.object(fc_cleanup, "_run_root", return_value=run_root), \
                 patch.object(fc_cleanup.subprocess, "run",
                              return_value=_proc("111\n")), \
                 patch.object(
                     fc_cleanup, "_process_args",
                     return_value=("firecracker", "--config-file",
                                   str(run_root / "live-a/config.json")),
                 ):
                plan = fc_cleanup.prepare_cleanup()
        self.assertEqual((), plan.vm_pids)
        self.assertEqual((str(run_root / "dead-b"),), plan.run_dirs)
        self.assertNotIn(str(run_root / "live-a"), plan.run_dirs)


class TestCleanupRemoval(unittest.TestCase):
    def test_cleanup_kills_and_rmtrees(self):
        plan = FirecrackerBottleCleanupPlan(
            vm_pids=(101,),
            run_dirs=("/run/dev-x",),
        )
        with patch.object(fc_cleanup, "prepare_cleanup", return_value=plan), \
             patch.object(fc_cleanup, "_run_root", return_value=Path("/run")), \
             patch.object(fc_cleanup, "_terminate_orphan") as terminate, \
             patch.object(fc_cleanup.shutil, "rmtree") as rmtree, \
             patch.object(fc_cleanup, "info"):
            fc_cleanup.cleanup(plan)
        terminate.assert_called_once_with(101, Path("/run"))
        rmtree.assert_called_once_with("/run/dev-x", ignore_errors=True)

    def test_cleanup_skips_resources_no_longer_in_refreshed_plan(self):
        preview = FirecrackerBottleCleanupPlan(
            vm_pids=(999,), run_dirs=("/run/reused",),
        )
        with patch.object(
            fc_cleanup, "prepare_cleanup",
            return_value=FirecrackerBottleCleanupPlan(),
        ), patch.object(fc_cleanup, "_terminate_orphan") as terminate, \
             patch.object(fc_cleanup.shutil, "rmtree") as rmtree:
            fc_cleanup.cleanup(preview)
        terminate.assert_not_called()
        rmtree.assert_not_called()

    def test_pidfd_prevents_pid_reuse_from_signalling_unrelated_process(self):
        with patch.object(fc_cleanup.os, "pidfd_open", return_value=7), \
             patch.object(
                 fc_cleanup.Path, "read_bytes",
                 return_value=b"/usr/bin/python\0worker.py\0",
             ), patch.object(fc_cleanup.signal, "pidfd_send_signal") as send, \
             patch.object(fc_cleanup.os, "close"):
            fc_cleanup._terminate_orphan(101, Path("/run"))
        send.assert_not_called()

    def test_pidfd_signals_revalidated_orphan(self):
        command = b"firecracker\0--config-file\0/run/gone/config.json\0"
        with patch.object(fc_cleanup.os, "pidfd_open", return_value=7), \
             patch.object(fc_cleanup.Path, "read_bytes", return_value=command), \
             patch.object(fc_cleanup.signal, "pidfd_send_signal") as send, \
             patch.object(fc_cleanup.os, "close"), patch.object(fc_cleanup, "info"):
            fc_cleanup._terminate_orphan(101, Path("/run"))
        send.assert_called_once_with(7, fc_cleanup.signal.SIGTERM)


class TestCleanupPlan(unittest.TestCase):
    def test_empty(self):
        self.assertTrue(FirecrackerBottleCleanupPlan().empty)
        self.assertFalse(FirecrackerBottleCleanupPlan(vm_pids=(1,)).empty)

    def test_print_empty(self):
        with patch("bot_bottle.backend.firecracker.bottle_cleanup_plan.info") as info:
            FirecrackerBottleCleanupPlan().print()
        info.assert_called_once()
        self.assertIn("nothing to remove", info.call_args.args[0])

    def test_print_lists_resources(self):
        plan = FirecrackerBottleCleanupPlan(
            vm_pids=(5,), run_dirs=("/r",),
        )
        with patch("bot_bottle.backend.firecracker.bottle_cleanup_plan.info") as info:
            plan.print()
        joined = " ".join(c.args[0] for c in info.call_args_list)
        self.assertIn("pid 5", joined)
        self.assertIn("run dir: /r", joined)


class TestBackendDelegates(unittest.TestCase):
    """Each backend's setup/status/teardown/cleanup classmethods forward
    to their module — assert the forwarding (and return-code passthrough)."""

    def test_firecracker_delegates(self):
        from bot_bottle.backend.firecracker import FirecrackerBottleBackend
        with patch("bot_bottle.backend.firecracker.setup.setup", return_value=1) as s, \
             patch("bot_bottle.backend.firecracker.setup.status", return_value=2), \
             patch("bot_bottle.backend.firecracker.setup.teardown", return_value=3):
            self.assertEqual(1, FirecrackerBottleBackend.setup())
            self.assertEqual(2, FirecrackerBottleBackend.status())
            self.assertEqual(3, FirecrackerBottleBackend.teardown())
        s.assert_called_once_with()
        with patch("bot_bottle.backend.firecracker.cleanup.prepare_cleanup") as pc, \
             patch("bot_bottle.backend.firecracker.cleanup.cleanup") as cl, \
             patch("bot_bottle.backend.firecracker.enumerate.enumerate_active",
                   return_value=[]):
            b = FirecrackerBottleBackend()
            b.prepare_cleanup()
            b.cleanup(FirecrackerBottleCleanupPlan())
            self.assertEqual([], b.enumerate_active())
        pc.assert_called_once_with()
        cl.assert_called_once()

    def test_docker_delegates(self):
        from bot_bottle.backend.docker import DockerBottleBackend
        with patch("bot_bottle.backend.docker.setup.setup", return_value=4), \
             patch("bot_bottle.backend.docker.setup.status", return_value=5), \
             patch("bot_bottle.backend.docker.setup.teardown", return_value=6):
            self.assertEqual(4, DockerBottleBackend.setup())
            self.assertEqual(5, DockerBottleBackend.status())
            self.assertEqual(6, DockerBottleBackend.teardown())

    def test_macos_delegates(self):
        from bot_bottle.backend.macos_container import MacosContainerBottleBackend
        with patch("bot_bottle.backend.macos_container.setup.setup", return_value=7), \
             patch("bot_bottle.backend.macos_container.setup.status", return_value=8), \
             patch("bot_bottle.backend.macos_container.setup.teardown", return_value=9):
            self.assertEqual(7, MacosContainerBottleBackend.setup())
            self.assertEqual(8, MacosContainerBottleBackend.status())
            self.assertEqual(9, MacosContainerBottleBackend.teardown())


if __name__ == "__main__":
    unittest.main()
