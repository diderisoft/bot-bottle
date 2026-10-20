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
from unittest.mock import patch

from tests.unit import use_bottle_root
from bot_bottle.contract.hostctl import Settings
from bot_bottle import bottle_state
from bot_bottle.backend import EnumerationError
from bot_bottle.backend.docker import cleanup
from bot_bottle.backend.docker.cleanup import _list_orphan_state_dirs


class _FakeHomeMixin:
    def _setup_fake_home(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="docker-cleanup-test.")
        self._restore = use_bottle_root(Path(self._tmp.name) / ".bot-bottle")

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
        # Preserve marker means the user explicitly wanted this dir
        # kept for `resume`.
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
        # any backend (firecracker included). docker's orphan
        # detection respects them so a running firecracker
        # bottle's state dir isn't reaped while the VM is up.
        bottle_state.write_per_bottle_dockerfile("fc-hhh", "FROM x\n")
        self.assertEqual(
            [],
            _list_orphan_state_dirs(set(), {"fc-hhh"}),
        )

    def test_protected_overrides_no_live_project(self):
        # A firecracker bottle has no docker compose project but
        # IS in the protected set; the absence of a project
        # shouldn't cause a reap.
        bottle_state.write_per_bottle_dockerfile("fc-iii", "FROM x\n")
        self.assertEqual(
            [],
            _list_orphan_state_dirs(
                {"bot-bottle-something-else"},  # different project up
                {"fc-iii"},                         # but fc-iii is live
            ),
        )


class TestAuthoritativeDiscovery(unittest.TestCase):
    def test_prepare_requires_authoritative_compose_projects(self):
        with patch.object(cleanup.docker_mod, "require_docker"), \
             patch.object(
                 cleanup, "list_compose_projects",
                 side_effect=EnumerationError("compose unavailable"),
             ) as projects, self.assertRaisesRegex(
                 EnumerationError, "compose unavailable",
             ):
            cleanup.prepare_cleanup(Settings())
        projects.assert_called_once_with(
            warn_on_error=False,
            raise_on_error=True,
        )

    def test_container_query_failure_raises(self):
        failed = cleanup.subprocess.CompletedProcess(
            [], 1, stdout="", stderr="daemon unavailable",
        )
        with patch.object(cleanup.subprocess, "run", return_value=failed), \
             self.assertRaisesRegex(EnumerationError, "daemon unavailable"):
            cleanup._list_prefixed_containers()

    def test_network_query_failure_raises(self):
        failed = cleanup.subprocess.CompletedProcess(
            [], 1, stdout="", stderr="daemon unavailable",
        )
        with patch.object(cleanup.subprocess, "run", return_value=failed), \
             self.assertRaisesRegex(EnumerationError, "daemon unavailable"):
            cleanup._list_prefixed_networks()


if __name__ == "__main__":
    unittest.main()
