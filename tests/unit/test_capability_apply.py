"""Unit: capability_apply helpers (PRD 0016 Phase 2).

docker cp / exec / rm / network rm paths are covered by the
integration test in Phase 4. Here we cover:
  - fetch_current_dockerfile fallback chain (per-bottle → repo)
  - apply_capability_change writes the per-bottle Dockerfile and
    returns the correct (before, after).
  - apply_capability_change rejects empty input.
"""

import tempfile
import unittest
from pathlib import Path

from claude_bottle import supervise
from claude_bottle.backend.docker import bottle_state, capability_apply
from claude_bottle.backend.docker.capability_apply import (
    CapabilityApplyError,
    apply_capability_change,
    fetch_current_dockerfile,
)


class _FakeHomeMixin:
    def _setup_fake_home(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cap-apply-test.")
        original = supervise.claude_bottle_root

        def fake_root() -> Path:
            return Path(self._tmp.name) / ".claude-bottle"

        supervise.claude_bottle_root = fake_root  # type: ignore[assignment]
        self._restore = lambda: setattr(supervise, "claude_bottle_root", original)

    def _teardown_fake_home(self):
        self._restore()
        self._tmp.cleanup()


class TestFetchCurrentDockerfile(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_returns_per_bottle_dockerfile_when_present(self):
        bottle_state.write_per_bottle_dockerfile("dev", "FROM rebuilt\n")
        self.assertEqual("FROM rebuilt\n", fetch_current_dockerfile("dev"))

    def test_falls_back_to_repo_dockerfile_when_no_override(self):
        # The repo's Dockerfile actually exists; the test just checks
        # we get its content (non-empty) when no per-bottle override
        # is set.
        content = fetch_current_dockerfile("dev-no-override")
        self.assertIn("FROM ", content)


class TestApplyCapabilityChange(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()
        # Stub out the docker-dependent helpers. The orchestrator's
        # job is to sequence write + snapshot + push + teardown; we
        # validate that sequence here, not the docker primitives.
        self._calls: list[str] = []
        self._orig_snapshot = capability_apply._snapshot_transcript
        self._orig_push = capability_apply._push_working_tree
        self._orig_teardown = capability_apply._teardown_bottle

        def stub_snapshot(slug):
            self._calls.append(f"snapshot:{slug}")

        def stub_push(slug):
            self._calls.append(f"push:{slug}")

        def stub_teardown(slug):
            self._calls.append(f"teardown:{slug}")

        capability_apply._snapshot_transcript = stub_snapshot  # type: ignore[assignment]
        capability_apply._push_working_tree = stub_push  # type: ignore[assignment]
        capability_apply._teardown_bottle = stub_teardown  # type: ignore[assignment]

    def tearDown(self):
        capability_apply._snapshot_transcript = self._orig_snapshot  # type: ignore[assignment]
        capability_apply._push_working_tree = self._orig_push  # type: ignore[assignment]
        capability_apply._teardown_bottle = self._orig_teardown  # type: ignore[assignment]
        self._teardown_fake_home()

    def test_writes_per_bottle_dockerfile_and_returns_before_after(self):
        bottle_state.write_per_bottle_dockerfile("dev", "FROM old\n")
        before, after = apply_capability_change("dev", "FROM new\nRUN apk add ripgrep\n")
        self.assertEqual("FROM old\n", before)
        self.assertEqual("FROM new\nRUN apk add ripgrep\n", after)
        self.assertEqual(
            "FROM new\nRUN apk add ripgrep\n",
            bottle_state.per_bottle_dockerfile("dev"),
        )

    def test_calls_snapshot_push_teardown_in_order(self):
        apply_capability_change("dev", "FROM new\n")
        # Snapshot + push must happen BEFORE write_per_bottle_dockerfile
        # (so they capture pre-rebuild state) and BEFORE teardown (so
        # the agent container still exists to docker exec / cp from).
        # Teardown must be last.
        self.assertEqual(
            ["snapshot:dev", "push:dev", "teardown:dev"],
            self._calls,
        )

    def test_first_change_falls_back_to_repo_dockerfile_for_before(self):
        # No per-bottle override yet — before-diff comes from the
        # repo's Dockerfile.
        before, after = apply_capability_change("dev-fresh", "FROM new\n")
        self.assertIn("FROM ", before)
        self.assertEqual("FROM new\n", after)

    def test_empty_dockerfile_rejected(self):
        with self.assertRaises(CapabilityApplyError):
            apply_capability_change("dev", "   \n\t\n")


if __name__ == "__main__":
    unittest.main()
