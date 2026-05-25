"""Unit: cli/cleanup.py state-dir enumeration + summary.

The end-to-end cleanup-with-prompt flow is exercised manually;
here we cover the state-dir display logic so a regression in the
resumable / orphan / rebuild flags surfaces in unit CI."""

import tempfile
import unittest
from pathlib import Path

from claude_bottle import supervise
from claude_bottle.backend.docker import bottle_state
from claude_bottle.cli.cleanup import _enumerate_state_dirs, _state_summary


class _FakeHomeMixin:
    def _setup_fake_home(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cli-cleanup-test.")
        original = supervise.claude_bottle_root

        def fake_root() -> Path:
            return Path(self._tmp.name) / ".claude-bottle"

        supervise.claude_bottle_root = fake_root  # type: ignore[assignment]
        self._restore = lambda: setattr(supervise, "claude_bottle_root", original)

    def _teardown_fake_home(self):
        self._restore()
        self._tmp.cleanup()


class TestEnumerateStateDirs(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_empty_when_no_state_root(self):
        self.assertEqual([], _enumerate_state_dirs())

    def test_lists_each_identity_dir(self):
        bottle_state.write_per_bottle_dockerfile("dev-aaa", "FROM x\n")
        bottle_state.write_per_bottle_dockerfile("api-bbb", "FROM y\n")
        dirs = _enumerate_state_dirs()
        self.assertEqual(
            ["api-bbb", "dev-aaa"],
            [p.name for p in dirs],
        )

    def test_sorted_for_stable_preflight(self):
        for name in ("z", "a", "m"):
            bottle_state.write_per_bottle_dockerfile(name, "FROM x\n")
        names = [p.name for p in _enumerate_state_dirs()]
        self.assertEqual(["a", "m", "z"], names)


class TestStateSummary(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def _path(self, name: str) -> Path:
        return bottle_state.bottle_state_dir(name)

    def test_orphan_state_dir(self):
        # Only a Dockerfile, no metadata.json — the "api / dev" shape
        # that comes from pre-identity-fix code.
        bottle_state.write_per_bottle_dockerfile("orphan", "FROM old\n")
        s = _state_summary(self._path("orphan"))
        self.assertIn("orphan", s)
        self.assertIn("no metadata.json", s)
        self.assertIn("rebuilt Dockerfile", s)

    def test_resumable_state_dir(self):
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity="dev-aaa", agent_name="dev",
            cwd="/proj/A", copy_cwd=True, started_at="t",
        ))
        s = _state_summary(self._path("dev-aaa"))
        self.assertIn("resumable", s)
        self.assertNotIn("rebuilt Dockerfile", s)

    def test_resumable_with_capability_rebuild_and_preserve_marker(self):
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity="dev-bbb", agent_name="dev",
            cwd="", copy_cwd=False, started_at="t",
        ))
        bottle_state.write_per_bottle_dockerfile("dev-bbb", "FROM rebuilt\n")
        bottle_state.transcript_snapshot_dir("dev-bbb").mkdir(parents=True)
        bottle_state.mark_preserved("dev-bbb")
        s = _state_summary(self._path("dev-bbb"))
        self.assertIn("resumable", s)
        self.assertIn("rebuilt Dockerfile", s)
        self.assertIn("transcript snapshot", s)
        self.assertIn("preserve marker", s)


if __name__ == "__main__":
    unittest.main()
