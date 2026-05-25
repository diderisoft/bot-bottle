"""Unit: per-bottle state helpers (PRD 0016 Phase 1) + identity +
launch metadata."""

import re
import tempfile
import unittest
from pathlib import Path

from claude_bottle import supervise
from claude_bottle.backend.docker import bottle_state
from claude_bottle.backend.docker.bottle_state import (
    BottleMetadata,
    read_metadata,
    write_metadata,
)


class _FakeHomeMixin:
    def _setup_fake_home(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bottle-state-test.")
        original = supervise.claude_bottle_root

        def fake_root() -> Path:
            return Path(self._tmp.name) / ".claude-bottle"

        supervise.claude_bottle_root = fake_root  # type: ignore[assignment]
        self._restore = lambda: setattr(supervise, "claude_bottle_root", original)

    def _teardown_fake_home(self):
        self._restore()
        self._tmp.cleanup()


class TestPerBottleDockerfile(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_returns_none_when_absent(self):
        self.assertIsNone(bottle_state.per_bottle_dockerfile("dev"))

    def test_write_then_read_roundtrip(self):
        bottle_state.write_per_bottle_dockerfile(
            "dev", "FROM python:3.13\nRUN apk add ripgrep\n",
        )
        self.assertEqual(
            "FROM python:3.13\nRUN apk add ripgrep\n",
            bottle_state.per_bottle_dockerfile("dev"),
        )

    def test_isolated_per_slug(self):
        bottle_state.write_per_bottle_dockerfile("dev", "FROM dev\n")
        bottle_state.write_per_bottle_dockerfile("api", "FROM api\n")
        self.assertEqual("FROM dev\n", bottle_state.per_bottle_dockerfile("dev"))
        self.assertEqual("FROM api\n", bottle_state.per_bottle_dockerfile("api"))

    def test_dockerfile_path_under_state_dir(self):
        path = bottle_state.per_bottle_dockerfile_path("dev")
        self.assertTrue(str(path).endswith("/.claude-bottle/state/dev/Dockerfile"))

    def test_image_tag_unique_per_slug(self):
        self.assertEqual(
            "claude-bottle-rebuilt-dev:latest",
            bottle_state.per_bottle_image_tag("dev"),
        )
        self.assertNotEqual(
            bottle_state.per_bottle_image_tag("dev"),
            bottle_state.per_bottle_image_tag("api"),
        )

    def test_transcript_dir_under_state_dir(self):
        path = bottle_state.transcript_snapshot_dir("dev")
        self.assertTrue(str(path).endswith("/.claude-bottle/state/dev/transcript"))


class TestBottleIdentity(unittest.TestCase):
    """bottle_identity(agent_name) — PRD 0016 follow-up.

    Every call mints a fresh identity with a random 5-char suffix
    so multiple instances of the same agent can run in parallel
    without container name collisions. The slug-prefix is for
    readability; the suffix is for uniqueness. To continue an
    existing bottle, use the recorded identity via
    `cli.py resume <identity>`, not this function."""

    def test_format_is_slug_dash_5_alnum(self):
        identity = bottle_state.bottle_identity("dev")
        self.assertTrue(identity.startswith("dev-"))
        suffix = identity[len("dev-"):]
        self.assertEqual(5, len(suffix))
        self.assertTrue(
            re.fullmatch(r"[a-z0-9]+", suffix),
            f"suffix {suffix!r} must be lowercase base36",
        )

    def test_two_calls_yield_different_identities(self):
        # 5-char base36 gives ~60M combinations; collision in two
        # calls is astronomically unlikely. If this ever flakes it's
        # almost certainly a regression, not a bad-luck collision.
        a = bottle_state.bottle_identity("dev")
        b = bottle_state.bottle_identity("dev")
        self.assertNotEqual(a, b)

    def test_different_agents_get_different_prefixes(self):
        a = bottle_state.bottle_identity("dev")
        b = bottle_state.bottle_identity("api")
        self.assertTrue(a.startswith("dev-"))
        self.assertTrue(b.startswith("api-"))

    def test_agent_name_slugified(self):
        identity = bottle_state.bottle_identity("My Agent")
        self.assertTrue(identity.startswith("my-agent-"))


class TestPreserveMarker(_FakeHomeMixin, unittest.TestCase):
    """The .preserve marker is how capability_apply tells cli.py's
    session-end cleanup to keep the state dir instead of removing it."""

    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_default_is_unpreserved(self):
        self.assertFalse(bottle_state.is_preserved("dev-x"))

    def test_mark_then_is_preserved(self):
        bottle_state.mark_preserved("dev-x")
        self.assertTrue(bottle_state.is_preserved("dev-x"))

    def test_clear_removes_marker(self):
        bottle_state.mark_preserved("dev-x")
        bottle_state.clear_preserve_marker("dev-x")
        self.assertFalse(bottle_state.is_preserved("dev-x"))

    def test_clear_is_idempotent(self):
        # No marker present — should not raise.
        bottle_state.clear_preserve_marker("never-existed")
        self.assertFalse(bottle_state.is_preserved("never-existed"))

    def test_marker_path_under_state_dir(self):
        path = bottle_state.preserve_marker_path("dev-x")
        self.assertTrue(str(path).endswith("/.claude-bottle/state/dev-x/.preserve"))


class TestCleanupState(_FakeHomeMixin, unittest.TestCase):
    """cleanup_state removes the entire per-bottle state dir.
    Called by cli.py when a session ends without the preserve marker."""

    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_removes_state_dir_and_contents(self):
        bottle_state.write_per_bottle_dockerfile("dev-x", "FROM x\n")
        d = bottle_state.bottle_state_dir("dev-x")
        self.assertTrue(d.is_dir())
        bottle_state.cleanup_state("dev-x")
        self.assertFalse(d.exists())

    def test_idempotent_when_dir_missing(self):
        # Never created — should not raise.
        bottle_state.cleanup_state("never-existed")


class TestBottleMetadata(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_read_missing_returns_none(self):
        self.assertIsNone(read_metadata("does-not-exist"))

    def test_write_then_read_roundtrip(self):
        meta = BottleMetadata(
            identity="dev-a4f8c",
            agent_name="dev",
            cwd="/proj/A",
            copy_cwd=True,
            started_at="2026-05-25T12:00:00+00:00",
        )
        write_metadata(meta)
        loaded = read_metadata("dev-a4f8c")
        self.assertEqual(meta, loaded)

    def test_metadata_lives_under_state_dir(self):
        meta = BottleMetadata(
            identity="dev-x", agent_name="dev",
            cwd="", copy_cwd=False, started_at="t",
        )
        path = write_metadata(meta)
        self.assertTrue(
            str(path).endswith("/.claude-bottle/state/dev-x/metadata.json"),
        )

    def test_overwriting_metadata_updates_timestamp(self):
        # `resume` re-writes metadata with a fresh started_at;
        # everything else stays the same.
        write_metadata(BottleMetadata(
            identity="dev-y", agent_name="dev",
            cwd="/proj/A", copy_cwd=True, started_at="t1",
        ))
        write_metadata(BottleMetadata(
            identity="dev-y", agent_name="dev",
            cwd="/proj/A", copy_cwd=True, started_at="t2",
        ))
        loaded = read_metadata("dev-y")
        assert loaded is not None
        self.assertEqual("t2", loaded.started_at)


if __name__ == "__main__":
    unittest.main()
