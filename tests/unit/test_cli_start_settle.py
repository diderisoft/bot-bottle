"""Unit: cli/start.py session-end state capture (crash preservation).

The launch-context machinery is covered by integration; this isolates
the post-exec_agent decision: snapshot transcript + mark for
preservation if non-zero exit, no-op for clean exit."""

import tempfile
import unittest
from pathlib import Path

from bot_bottle import supervise
from bot_bottle import bottle_state
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
    # snapshot_transcript is commented out (capability_apply is disabled);
    # capture_claude_session_state now only handles the preserve marker.
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_clean_exit_does_not_mark(self):
        start_mod.capture_claude_session_state("dev-abc", exit_code=0)
        self.assertFalse(bottle_state.is_preserved("dev-abc"))

    def test_crash_marks_preserved(self):
        start_mod.capture_claude_session_state("dev-abc", exit_code=137)
        self.assertTrue(bottle_state.is_preserved("dev-abc"))

    def test_ctrl_c_treated_as_crash(self):
        # SIGINT delivers exit 130; the operator may have Ctrl-C'd
        # because something went wrong, so we preserve.
        start_mod.capture_claude_session_state("dev-abc", exit_code=130)
        self.assertTrue(bottle_state.is_preserved("dev-abc"))

    def test_empty_identity_is_noop(self):
        # Backends without an identity field shouldn't crash this
        # path (the _identity_from_plan helper falls back to "").
        start_mod.capture_claude_session_state("", exit_code=137)
        self.assertFalse(bottle_state.is_preserved(""))


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
