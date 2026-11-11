"""Unit: backend/state_cleanup — left-over bottled agents, shared by every
backend.

A bottled agent is reaped when its bottle isn't live in any backend, it isn't
preserved, and the backend it records was listed (so whether it's live is
known), or it records no backend. A state dir no row records is reaped
unless its bottle is live; reaping one deletes its state dir and its rows
together."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot_bottle import bottle_state
from bot_bottle.backend.state_cleanup import StateCleanupPlan, cleanup_state, orphans
from bot_bottle.contract.hostctl import Settings
from tests.unit import use_bottle_root

_ALL = {"docker", "firecracker", "macos-container"}


def _state(identity: str, backend: str = "docker") -> None:
    bottle_state.write_per_bottle_dockerfile(identity, "FROM x\n")
    bottle_state.write_metadata(bottle_state.BottleMetadata(
        identity=identity, agent_name="pi", cwd="", copy_cwd=False,
        started_at="2026-10-24T00:00:00Z", backend=backend))


class TestOrphans(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="state-cleanup-test.")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(use_bottle_root(Path(tmp.name) / ".bot-bottle"))

    def test_nothing_recorded_returns_empty(self):
        self.assertEqual([], orphans(set(), _ALL))

    def test_a_state_dir_without_a_row_is_one_unless_live(self):
        # Nothing can resume it without a row, preserved or not.
        bottle_state.write_per_bottle_dockerfile("stray", "FROM x\n")
        bottle_state.write_per_bottle_dockerfile("kept-stray", "FROM x\n")
        bottle_state.mark_preserved("kept-stray")
        bottle_state.write_per_bottle_dockerfile("running", "FROM x\n")
        self.assertEqual(["kept-stray", "stray"], orphans({"running"}, _ALL))
        # With no backend listed, whether it's live isn't known.
        self.assertEqual([], orphans(set(), set()))

    def test_one_recording_no_backend_is_a_candidate(self):
        _state("nowhere-1", "")
        self.assertEqual(["nowhere-1"], orphans(set(), {"docker"}))
        self.assertEqual([], orphans({"nowhere-1"}, {"docker"}))

    def test_cleanup_removes_a_state_dir_without_a_row(self):
        bottle_state.write_per_bottle_dockerfile("stray", "FROM x\n")
        cleanup_state(StateCleanupPlan(("stray",)), Settings())
        self.assertFalse(bottle_state.bottle_state_dir("stray").exists())

    def test_one_with_nothing_live_and_no_preserve_is_an_orphan(self):
        _state("solo-aaa")
        self.assertEqual(["solo-aaa"], orphans(set(), _ALL))

    def test_a_live_bottle_in_any_backend_is_kept(self):
        _state("fc-hhh", "firecracker")
        _state("mac-iii", "macos-container")
        self.assertEqual([], orphans({"fc-hhh", "mac-iii"}, _ALL))

    def test_preserve_keeps_it(self):
        _state("kept-ccc")
        bottle_state.mark_preserved("kept-ccc")
        self.assertEqual([], orphans(set(), _ALL))

    def test_mixed_and_sorted(self):
        for name in ("zzz-1", "aaa-1", "live-fff", "kept-ggg"):
            _state(name)
        bottle_state.mark_preserved("kept-ggg")
        self.assertEqual(["aaa-1", "zzz-1"], orphans({"live-fff"}, _ALL))

    def test_an_unlisted_backends_are_left_alone(self):
        # Docker's daemon is down: its bottles might be live, so their
        # state stays; the macos-container one (listed, not live) goes.
        _state("dock-1", "docker")
        _state("mac-1", "macos-container")
        self.assertEqual(["mac-1"], orphans(set(), {"macos-container"}))

    def test_cleanup_removes_the_planned_dirs_and_rows(self):
        _state("gone-1")
        _state("stays-1")
        cleanup_state(StateCleanupPlan(("gone-1",)), Settings())
        self.assertFalse(bottle_state.bottle_state_dir("gone-1").exists())
        self.assertIsNone(bottle_state.read_metadata("gone-1"))
        self.assertTrue(bottle_state.bottle_state_dir("stays-1").exists())
        self.assertIsNotNone(bottle_state.read_metadata("stays-1"))

    def test_intersect_keeps_only_what_is_still_an_orphan(self):
        shown = StateCleanupPlan(("a", "b"))
        self.assertEqual(("b",), shown.intersect(StateCleanupPlan(("b", "c"))).identities)


if __name__ == "__main__":
    unittest.main()
