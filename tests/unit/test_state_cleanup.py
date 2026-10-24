"""Unit: backend/state_cleanup — orphan state dirs, shared by every backend.

A state dir is reaped when its bottle isn't live in any backend, it isn't
preserved, and the backend it records was listed (so whether it's live is
known). Older dirs without a recorded backend are Docker's."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot_bottle import bottle_state
from bot_bottle.backend.state_cleanup import (
    StateCleanupPlan, cleanup_state, orphan_state_dirs, state_backend,
)
from bot_bottle.contract.hostctl import Settings
from tests.unit import use_bottle_root

_ALL = {"docker", "firecracker", "macos-container"}


def _state(identity: str, backend: str = "") -> None:
    bottle_state.write_per_bottle_dockerfile(identity, "FROM x\n")
    if backend:
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity=identity, agent_name="pi", cwd="", copy_cwd=False,
            started_at="2026-10-24T00:00:00Z", backend=backend))


class TestOrphanStateDirs(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="state-cleanup-test.")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(use_bottle_root(Path(tmp.name) / ".bot-bottle"))

    def test_no_state_root_returns_empty(self):
        self.assertEqual([], orphan_state_dirs(set(), _ALL))

    def test_a_dir_with_nothing_live_and_no_preserve_is_an_orphan(self):
        _state("solo-aaa")
        self.assertEqual(["solo-aaa"], orphan_state_dirs(set(), _ALL))

    def test_a_live_bottle_in_any_backend_keeps_its_dir(self):
        _state("fc-hhh", "firecracker")
        _state("mac-iii", "macos-container")
        self.assertEqual([], orphan_state_dirs({"fc-hhh", "mac-iii"}, _ALL))

    def test_preserve_keeps_it(self):
        _state("kept-ccc")
        bottle_state.mark_preserved("kept-ccc")
        self.assertEqual([], orphan_state_dirs(set(), _ALL))

    def test_mixed_and_sorted(self):
        for name in ("zzz-1", "aaa-1", "live-fff", "kept-ggg"):
            _state(name)
        bottle_state.mark_preserved("kept-ggg")
        self.assertEqual(["aaa-1", "zzz-1"], orphan_state_dirs({"live-fff"}, _ALL))

    def test_an_unlisted_backends_dirs_are_left_alone(self):
        # Docker's daemon is down: its bottles might be live, so their
        # state stays; the macos-container one (listed, not live) goes.
        _state("old-docker")              # no recorded backend: docker's
        _state("dock-1", "docker")
        _state("mac-1", "macos-container")
        self.assertEqual(["mac-1"], orphan_state_dirs(set(), {"macos-container"}))

    def test_the_recorded_backend(self):
        _state("old")
        _state("vm", "firecracker")
        self.assertEqual(("docker", "firecracker"), (state_backend("old"), state_backend("vm")))

    def test_cleanup_removes_the_planned_dirs(self):
        _state("gone-1")
        _state("stays-1")
        cleanup_state(StateCleanupPlan(("gone-1",)), Settings())
        self.assertFalse(bottle_state.bottle_state_dir("gone-1").exists())
        self.assertTrue(bottle_state.bottle_state_dir("stays-1").exists())

    def test_intersect_keeps_only_what_is_still_an_orphan(self):
        shown = StateCleanupPlan(("a", "b"))
        self.assertEqual(("b",), shown.intersect(StateCleanupPlan(("b", "c"))).identities)


if __name__ == "__main__":
    unittest.main()
