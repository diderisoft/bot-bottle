"""Unit: backend/docker/cleanup orphan-state-dir detection.

PRD 0018 chunk 4. The orphan-state-dir rule has three categories:
  - LIVE: a compose project with the matching name is up → keep
  - PRESERVED: state dir carries `.preserve` → keep (resume target)
  - ORPHAN: neither → reap

These are the cases the test exercises. The compose-project +
container/network enumeration is left to the integration tests
because it requires a real docker daemon."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot_bottle import supervise
from bot_bottle import bottle_state
from bot_bottle.backend.docker.cleanup import _list_orphan_state_dirs


class _FakeHomeMixin:
    def _setup_fake_home(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="docker-cleanup-test.")
        original = supervise.bot_bottle_root

        def fake_root() -> Path:
            return Path(self._tmp.name) / ".bot-bottle"

        supervise.bot_bottle_root = fake_root  # type: ignore[assignment]
        self._restore = lambda: setattr(supervise, "bot_bottle_root", original)

    def _teardown_fake_home(self) -> None:
        self._restore()
        self._tmp.cleanup()


class TestOrphanStateDirs(_FakeHomeMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._setup_fake_home()

    def tearDown(self) -> None:
        self._teardown_fake_home()

    def test_no_state_root_returns_empty(self):
        self.assertEqual([], _list_orphan_state_dirs(set(), set()))

    def test_state_dir_with_no_live_no_preserve_is_orphan(self):
        # Just touch the dir; no metadata, no preserve marker — the
        # exact orphan shape.
        bottle_state.write_per_bottle_dockerfile("solo-aaa", "FROM x\n")
        self.assertEqual(
            ["solo-aaa"],
            _list_orphan_state_dirs(set(), set()),
        )

    def test_live_project_skips_dir(self):
        # Live project means the bottle is currently running under
        # compose — never reap.
        bottle_state.write_per_bottle_dockerfile("live-bbb", "FROM x\n")
        self.assertEqual(
            [],
            _list_orphan_state_dirs({"bot-bottle-live-bbb"}, set()),
        )

    def test_preserve_marker_skips_dir(self):
        # Preserve marker = capability-block or crash auto-preserve;
        # the user explicitly wanted this dir kept for `resume`.
        bottle_state.write_per_bottle_dockerfile("kept-ccc", "FROM x\n")
        bottle_state.mark_preserved("kept-ccc")
        self.assertEqual(
            [],
            _list_orphan_state_dirs(set(), set()),
        )

    def test_preserve_overrides_no_live_project(self):
        # Even without a live project, a preserve marker keeps it.
        bottle_state.write_per_bottle_dockerfile("kept-ddd", "FROM x\n")
        bottle_state.mark_preserved("kept-ddd")
        self.assertEqual([], _list_orphan_state_dirs(set(), set()))

    def test_mixed_set_categorized_correctly(self):
        bottle_state.write_per_bottle_dockerfile("orphan-eee", "FROM x\n")
        bottle_state.write_per_bottle_dockerfile("live-fff", "FROM y\n")
        bottle_state.write_per_bottle_dockerfile("kept-ggg", "FROM z\n")
        bottle_state.mark_preserved("kept-ggg")

        result = _list_orphan_state_dirs({"bot-bottle-live-fff"}, set())
        self.assertEqual(["orphan-eee"], result)

    def test_sorted_output(self):
        for name in ("zzz-1", "aaa-1", "mmm-1"):
            bottle_state.write_per_bottle_dockerfile(name, "FROM x\n")
        self.assertEqual(
            ["aaa-1", "mmm-1", "zzz-1"],
            _list_orphan_state_dirs(set(), set()),
        )

    def test_protected_identity_skips_dir(self):
        # `protected_identities` carries slugs that are live in
        # any backend (smolmachines included). docker's orphan
        # detection respects them so a running smolmachines
        # bottle's state dir isn't reaped while the VM is up.
        bottle_state.write_per_bottle_dockerfile("smol-hhh", "FROM x\n")
        self.assertEqual(
            [],
            _list_orphan_state_dirs(set(), {"smol-hhh"}),
        )

    def test_protected_overrides_no_live_project(self):
        # A smolmachines bottle has no docker compose project but
        # IS in the protected set; the absence of a project
        # shouldn't cause a reap.
        bottle_state.write_per_bottle_dockerfile("smol-iii", "FROM x\n")
        self.assertEqual(
            [],
            _list_orphan_state_dirs(
                {"bot-bottle-something-else"},  # different project up
                {"smol-iii"},                       # but smol-iii is live
            ),
        )


if __name__ == "__main__":
    unittest.main()
