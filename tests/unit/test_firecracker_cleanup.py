"""Unit: Firecracker cleanup (orphan enumeration + removal) and the
thin backend classmethod delegates.

Cleanup shells out to pgrep/docker and touches the run dir; mock those so
the orphan-discovery + removal logic is covered without a live host. The
delegate tests just assert each backend's setup/status/teardown/cleanup
classmethods forward to their module.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from bot_bottle.backend.firecracker import cleanup as fc_cleanup
from bot_bottle.backend.firecracker.bottle_cleanup_plan import (
    FirecrackerBottleCleanupPlan,
)


def _proc(stdout: str = "", returncode: int = 0) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class TestOrphanEnumeration(unittest.TestCase):
    def test_orphan_vm_pids_filters_by_run_dir(self):
        run_root = str(fc_cleanup._run_root())
        out = (
            f"111 firecracker --config-file {run_root}/dev-a/config.json\n"
            "222 firecracker --config-file /somewhere/else/config.json\n"
            "notanint firecracker --config-file " + run_root + "/x\n"
        )
        with patch.object(fc_cleanup.subprocess, "run", return_value=_proc(out)):
            self.assertEqual([111], fc_cleanup._orphan_vm_pids())

    def test_orphan_vm_pids_empty_when_pgrep_fails(self):
        with patch.object(fc_cleanup.subprocess, "run", return_value=_proc(returncode=1)):
            self.assertEqual([], fc_cleanup._orphan_vm_pids())

    def test_sidecar_containers_sorted(self):
        with patch.object(fc_cleanup.subprocess, "run",
                          return_value=_proc("bot-bottle-sidecars-b\nbot-bottle-sidecars-a\n")):
            self.assertEqual(
                ["bot-bottle-sidecars-a", "bot-bottle-sidecars-b"],
                fc_cleanup._sidecar_containers(),
            )

    def test_sidecar_containers_empty_on_failure(self):
        with patch.object(fc_cleanup.subprocess, "run", return_value=_proc(returncode=1)):
            self.assertEqual([], fc_cleanup._sidecar_containers())

    def test_run_dirs_empty_when_absent(self):
        with patch.object(fc_cleanup.util, "cache_dir") as cache:
            cache.return_value.__truediv__.return_value.is_dir.return_value = False
            self.assertEqual([], fc_cleanup._run_dirs())

    def test_prepare_cleanup_assembles_plan(self):
        with patch.object(fc_cleanup, "_orphan_vm_pids", return_value=[7]), \
             patch.object(fc_cleanup, "_sidecar_containers", return_value=["c1"]), \
             patch.object(fc_cleanup, "_run_dirs", return_value=["/run/x"]):
            plan = fc_cleanup.prepare_cleanup()
        self.assertEqual((7,), plan.vm_pids)
        self.assertEqual(("c1",), plan.containers)
        self.assertEqual(("/run/x",), plan.run_dirs)


class TestCleanupRemoval(unittest.TestCase):
    def test_cleanup_kills_removes_and_rmtrees(self):
        plan = FirecrackerBottleCleanupPlan(
            vm_pids=(101,), containers=("bot-bottle-sidecars-x",),
            run_dirs=("/run/dev-x",),
        )
        with patch.object(fc_cleanup.os, "kill") as kill, \
             patch.object(fc_cleanup.subprocess, "run") as run, \
             patch.object(fc_cleanup.shutil, "rmtree") as rmtree, \
             patch.object(fc_cleanup, "info"):
            fc_cleanup.cleanup(plan)
        kill.assert_called_once()
        run.assert_called_once()
        self.assertIn("bot-bottle-sidecars-x", run.call_args.args[0])
        rmtree.assert_called_once_with("/run/dev-x", ignore_errors=True)

    def test_cleanup_tolerates_dead_pid(self):
        plan = FirecrackerBottleCleanupPlan(vm_pids=(999,))
        with patch.object(fc_cleanup.os, "kill", side_effect=ProcessLookupError), \
             patch.object(fc_cleanup, "info"):
            fc_cleanup.cleanup(plan)  # must not raise


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
            vm_pids=(5,), containers=("c",), run_dirs=("/r",),
        )
        with patch("bot_bottle.backend.firecracker.bottle_cleanup_plan.info") as info:
            plan.print()
        joined = " ".join(c.args[0] for c in info.call_args_list)
        self.assertIn("pid 5", joined)
        self.assertIn("container: c", joined)
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
