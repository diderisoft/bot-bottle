"""Unit: per-bottle state helpers (PRD 0016 Phase 1) + identity."""

import re
import tempfile
import unittest
from pathlib import Path

from claude_bottle import supervise
from claude_bottle.backend.docker import bottle_state


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
    """bottle_identity(agent_name, cwd) — PRD 0016 follow-up.

    Without --cwd, identity == slugify(agent_name) so existing
    no-cwd bottles look unchanged. With --cwd, identity has a
    cwd-hash suffix so the same agent against different projects
    gets distinct container / queue / audit / state dirs."""

    def test_no_cwd_returns_slug(self):
        self.assertEqual("dev", bottle_state.bottle_identity("dev", None))
        self.assertEqual("api-foo", bottle_state.bottle_identity("Api Foo", None))

    def test_cwd_appends_hash_suffix(self):
        identity = bottle_state.bottle_identity("dev", Path("/proj/A"))
        self.assertTrue(identity.startswith("dev-"))
        suffix = identity[len("dev-"):]
        self.assertEqual(12, len(suffix))
        self.assertTrue(re.fullmatch(r"[0-9a-f]+", suffix), suffix)

    def test_same_cwd_same_identity(self):
        a = bottle_state.bottle_identity("dev", Path("/proj/A"))
        b = bottle_state.bottle_identity("dev", Path("/proj/A"))
        self.assertEqual(a, b)

    def test_different_cwds_differ(self):
        a = bottle_state.bottle_identity("dev", Path("/proj/A"))
        b = bottle_state.bottle_identity("dev", Path("/proj/B"))
        self.assertNotEqual(a, b)

    def test_different_agents_same_cwd_differ(self):
        a = bottle_state.bottle_identity("dev", Path("/proj/A"))
        b = bottle_state.bottle_identity("api", Path("/proj/A"))
        self.assertNotEqual(a, b)

    def test_agent_name_slugified(self):
        # Identity's agent-name prefix is slugify(name), not the raw
        # name — same rule the rest of the codebase has always used.
        self.assertEqual(
            "my-agent",
            bottle_state.bottle_identity("My Agent", None),
        )


if __name__ == "__main__":
    unittest.main()
