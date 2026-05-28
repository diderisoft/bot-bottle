"""Unit: cli/start.py session-end state capture (crash preservation).

The launch-context machinery is covered by integration; this isolates
the post-exec_claude decision: snapshot transcript + mark for
preservation if non-zero exit, no-op for clean exit."""

import tempfile
import unittest
from pathlib import Path

from bot_bottle import supervise
from bot_bottle.backend.docker import bottle_state
from bot_bottle.cli import start as start_mod


class _FakeHomeMixin:
    def _setup_fake_home(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cli-start-settle.")
        self._original = supervise.bot_bottle_root

        def fake_root() -> Path:
            return Path(self._tmp.name) / ".bot-bottle"

        supervise.bot_bottle_root = fake_root  # type: ignore[assignment]

    def _teardown_fake_home(self):
        supervise.bot_bottle_root = self._original  # type: ignore[assignment]
        self._tmp.cleanup()


class TestCaptureSessionState(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()
        # Stub the docker-dependent snapshot call so this stays a
        # unit test. apply_capability_change's integration test
        # covers the real docker cp path.
        self._snap_calls: list[str] = []
        self._orig_snap = start_mod.snapshot_transcript
        start_mod.snapshot_transcript = lambda identity: (
            self._snap_calls.append(identity)
        )

    def tearDown(self):
        start_mod.snapshot_transcript = self._orig_snap
        self._teardown_fake_home()

    def test_clean_exit_snapshots_but_does_not_mark(self):
        start_mod.capture_session_state("dev-abc", exit_code=0)
        self.assertEqual(["dev-abc"], self._snap_calls)
        self.assertFalse(bottle_state.is_preserved("dev-abc"))

    def test_crash_snapshots_and_marks(self):
        start_mod.capture_session_state("dev-abc", exit_code=137)
        self.assertEqual(["dev-abc"], self._snap_calls)
        self.assertTrue(bottle_state.is_preserved("dev-abc"))

    def test_ctrl_c_treated_as_crash(self):
        # SIGINT delivers exit 130; the operator may have Ctrl-C'd
        # because something went wrong, so we preserve.
        start_mod.capture_session_state("dev-abc", exit_code=130)
        self.assertTrue(bottle_state.is_preserved("dev-abc"))

    def test_empty_identity_is_noop(self):
        # Backends without an identity field shouldn't crash this
        # path (the _identity_from_plan helper falls back to "").
        start_mod.capture_session_state("", exit_code=137)
        self.assertEqual([], self._snap_calls)


class TestSettleState(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_preserved_state_survives(self):
        bottle_state.write_per_bottle_dockerfile("dev-abc", "FROM x\n")
        bottle_state.mark_preserved("dev-abc")
        start_mod.settle_state("dev-abc")
        self.assertTrue(bottle_state.bottle_state_dir("dev-abc").is_dir())

    def test_unpreserved_state_is_cleaned(self):
        bottle_state.write_per_bottle_dockerfile("dev-abc", "FROM x\n")
        start_mod.settle_state("dev-abc")
        self.assertFalse(bottle_state.bottle_state_dir("dev-abc").exists())

    def test_empty_identity_is_noop(self):
        start_mod.settle_state("")  # should not raise


if __name__ == "__main__":
    unittest.main()
