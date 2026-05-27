"""Unit: smolmachines backend cleanup (`cleanup.py` +
`bottle_cleanup_plan.py`).

Tests mock `subprocess.run` + `has_backend` so they execute
without docker / smolvm on PATH. Each cleanup step verifies argv
shape; teardown verifies order (machines → bundles → networks)."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from claude_bottle import backend as backend_mod
from claude_bottle.backend.smolmachines import cleanup
from claude_bottle.backend.smolmachines.bottle_cleanup_plan import (
    SmolmachinesBottleCleanupPlan,
)


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=stderr,
    )


class TestPrepareCleanup(unittest.TestCase):
    def test_empty_when_nothing_running(self):
        with patch.object(cleanup, "_smolvm") as smolvm, \
             patch.object(cleanup.subprocess, "run") as run, \
             patch.object(backend_mod, "has_backend", return_value=True):
            smolvm.is_available.return_value = True
            run.return_value = _ok(stdout="[]")
            plan = cleanup.prepare_cleanup()
        self.assertTrue(plan.empty)

    def test_lists_machines_bundles_networks(self):
        def fake_run(argv, *a, **kw):
            if argv[:3] == ["smolvm", "machine", "ls"]:
                return _ok(stdout=(
                    '[{"name":"claude-bottle-a-1","state":"running"},'
                    ' {"name":"claude-bottle-b-2","state":"created"},'
                    ' {"name":"unrelated","state":"running"}]'
                ))
            if argv[:2] == ["docker", "ps"]:
                return _ok(stdout=(
                    "claude-bottle-sidecars-a-1\n"
                    "claude-bottle-sidecars-b-2\n"
                ))
            if argv[:3] == ["docker", "network", "ls"]:
                return _ok(stdout=(
                    "claude-bottle-bundle-a-1\n"
                    "claude-bottle-bundle-b-2\n"
                ))
            return _ok()

        with patch.object(cleanup, "_smolvm") as smolvm, \
             patch.object(cleanup.subprocess, "run", side_effect=fake_run), \
             patch.object(backend_mod, "has_backend", return_value=True):
            smolvm.is_available.return_value = True
            plan = cleanup.prepare_cleanup()

        # `unrelated` filtered out (no claude-bottle- prefix).
        self.assertEqual(
            ("claude-bottle-a-1", "claude-bottle-b-2"),
            plan.machines,
        )
        self.assertEqual(
            ("claude-bottle-sidecars-a-1", "claude-bottle-sidecars-b-2"),
            plan.bundles,
        )
        self.assertEqual(
            ("claude-bottle-bundle-a-1", "claude-bottle-bundle-b-2"),
            plan.networks,
        )

    def test_no_smolvm_means_no_machines(self):
        with patch.object(cleanup, "_smolvm") as smolvm, \
             patch.object(cleanup.subprocess, "run", return_value=_ok()), \
             patch.object(backend_mod, "has_backend", return_value=True):
            smolvm.is_available.return_value = False
            plan = cleanup.prepare_cleanup()
        self.assertEqual((), plan.machines)


class TestCleanup(unittest.TestCase):
    def test_machines_stopped_then_deleted_then_bundles_then_networks(self):
        plan = SmolmachinesBottleCleanupPlan(
            machines=("claude-bottle-a-1",),
            bundles=("claude-bottle-sidecars-a-1",),
            networks=("claude-bottle-bundle-a-1",),
        )
        calls: list[list[str]] = []

        def fake_run(argv, *a, **kw):
            calls.append(list(argv[:4]))
            return _ok()

        with patch.object(cleanup.subprocess, "run", side_effect=fake_run):
            cleanup.cleanup(plan)

        # Stop precedes delete precedes bundle rm precedes network rm.
        self.assertEqual(
            ["smolvm", "machine", "stop", "--name"], calls[0],
        )
        self.assertEqual(
            ["smolvm", "machine", "delete", "-f"], calls[1],
        )
        self.assertEqual(
            ["docker", "rm", "-f", "claude-bottle-sidecars-a-1"], calls[2],
        )
        self.assertEqual(
            ["docker", "network", "rm", "claude-bottle-bundle-a-1"], calls[3],
        )

    def test_failures_are_warnings_not_fatal(self):
        # smolvm machine delete -f returning non-zero should warn
        # but continue with bundles + networks. The cleanup is
        # idempotent on success and tries to remove every resource.
        plan = SmolmachinesBottleCleanupPlan(
            machines=("claude-bottle-a-1",),
            bundles=("claude-bottle-sidecars-a-1",),
            networks=(),
        )
        results = iter([
            _ok(),  # stop succeeds
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom"),  # delete fails
            _ok(),  # bundle rm succeeds
        ])

        def fake_run(argv, *a, **kw):
            return next(results)

        with patch.object(cleanup.subprocess, "run", side_effect=fake_run), \
             patch.object(cleanup, "warn") as warn:
            cleanup.cleanup(plan)
        # warn called once for the delete failure.
        warn.assert_called_once()

    def test_empty_plan_is_noop(self):
        plan = SmolmachinesBottleCleanupPlan()
        with patch.object(cleanup.subprocess, "run") as run:
            cleanup.cleanup(plan)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
