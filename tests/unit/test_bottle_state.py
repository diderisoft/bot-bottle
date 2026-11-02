"""Unit: per-bottle state helpers (PRD 0016 Phase 1) + identity +
launch metadata."""

import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

from tests.unit import use_bottle_root
from bot_bottle import bottle_state
from bot_bottle.bottle_state import (
    BottleMetadata,
    metadata_path,
    read_metadata,
    write_metadata,
)


class _FakeHomeMixin:
    def _setup_fake_home(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bottle-state-test.")
        self._restore = use_bottle_root(Path(self._tmp.name) / ".bot-bottle")

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
        self.assertTrue(str(path).endswith("/.bot-bottle/state/dev/Dockerfile"))

class TestBottleIdentity(unittest.TestCase):
    """bottle_identity() — mints a UUID v4 as the stable bottle key.

    The label (human name) is stored separately in metadata; the identity
    is a UUID so concurrent starts never collide and rename is a no-op."""

    def test_format_is_uuid_v4(self):
        identity = bottle_state.bottle_identity("dev")
        parsed = uuid.UUID(identity)
        self.assertEqual(4, parsed.version)

    def test_two_calls_yield_different_identities(self):
        a = bottle_state.bottle_identity("dev")
        b = bottle_state.bottle_identity("dev")
        self.assertNotEqual(a, b)

    def test_agent_name_does_not_affect_identity_format(self):
        # agent_name is accepted for backward compatibility but ignored.
        a = bottle_state.bottle_identity("dev")
        b = bottle_state.bottle_identity("My Totally Different Agent")
        self.assertRegex(a, r"^[0-9a-f-]{36}$")
        self.assertRegex(b, r"^[0-9a-f-]{36}$")

    def test_identity_accepted_by_launch_identity_check(self):
        import re
        _IDENTITY = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
        identity = bottle_state.bottle_identity()
        self.assertTrue(_IDENTITY.match(identity), f"{identity!r} must match _IDENTITY regex")


class TestPreserveMarker(_FakeHomeMixin, unittest.TestCase):
    """The .preserve marker tells cli.py's session-end cleanup to keep
    the state dir instead of removing it."""

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
        self.assertTrue(str(path).endswith("/.bot-bottle/state/dev-x/.preserve"))


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

    def test_a_bottle_that_mounted_its_project_resumes_with_a_copy(self):
        path = metadata_path("dev-m")
        path.parent.mkdir(parents=True)
        path.write_text('{"agent_name": "dev", "cwd": "/proj/A", "copy_cwd": false, '
                        '"mount_cwd": true, "started_at": "t"}')
        loaded = read_metadata("dev-m")
        assert loaded is not None
        self.assertEqual(("/proj/A", True), (loaded.cwd, loaded.copy_cwd))

    def test_metadata_lives_under_state_dir(self):
        meta = BottleMetadata(
            identity="dev-x", agent_name="dev",
            cwd="", copy_cwd=False, started_at="t",
        )
        path = write_metadata(meta)
        self.assertTrue(
            str(path).endswith("/.bot-bottle/state/dev-x/metadata.json"),
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


class TestBottleMetadataBackend(_FakeHomeMixin, unittest.TestCase):
    """PRD 0040: backend field is persisted and read back."""

    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_backend_field_roundtrips_docker(self):
        meta = BottleMetadata(
            identity="dev-b1",
            agent_name="dev",
            cwd="",
            copy_cwd=False,
            started_at="2026-06-02T00:00:00+00:00",
            compose_project="bot-bottle-dev-b1",
            backend="docker",
        )
        write_metadata(meta)
        loaded = read_metadata("dev-b1")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual("docker", loaded.backend)

    def test_backend_field_roundtrips_firecracker(self):
        meta = BottleMetadata(
            identity="dev-b2",
            agent_name="dev",
            cwd="",
            copy_cwd=False,
            started_at="2026-06-02T00:00:00+00:00",
            compose_project="",
            backend="firecracker",
        )
        write_metadata(meta)
        loaded = read_metadata("dev-b2")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual("firecracker", loaded.backend)

    def test_missing_backend_field_defaults_to_empty(self):
        # Old state dirs written before PRD 0040 have no backend key.
        import json
        from bot_bottle import bottle_state as bs
        path = bs.metadata_path("dev-b3")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "identity": "dev-b3",
            "agent_name": "dev",
            "cwd": "",
            "copy_cwd": False,
            "started_at": "2026-06-02T00:00:00+00:00",
            "compose_project": "bot-bottle-dev-b3",
        }))
        loaded = read_metadata("dev-b3")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual("", loaded.backend)


class TestCommittedImage(_FakeHomeMixin, unittest.TestCase):
    """write_committed_image / read_committed_image round-trip."""

    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_returns_none_when_absent(self):
        self.assertIsNone(bottle_state.read_committed_image("dev"))

    def test_write_then_read_roundtrip(self):
        bottle_state.write_committed_image("dev", "bot-bottle-committed-dev:latest")
        self.assertEqual(
            "bot-bottle-committed-dev:latest",
            bottle_state.read_committed_image("dev"),
        )

    def test_strips_trailing_newline_on_read(self):
        path = bottle_state.committed_image_path("dev")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("bot-bottle-committed-dev:latest\n\n")
        self.assertEqual(
            "bot-bottle-committed-dev:latest",
            bottle_state.read_committed_image("dev"),
        )

    def test_isolated_per_slug(self):
        bottle_state.write_committed_image("dev", "bot-bottle-committed-dev:latest")
        bottle_state.write_committed_image("api", "bot-bottle-committed-api:latest")
        self.assertEqual(
            "bot-bottle-committed-dev:latest",
            bottle_state.read_committed_image("dev"),
        )
        self.assertEqual(
            "bot-bottle-committed-api:latest",
            bottle_state.read_committed_image("api"),
        )

    def test_path_under_state_dir(self):
        path = bottle_state.committed_image_path("dev")
        self.assertTrue(str(path).endswith("/.bot-bottle/state/dev/committed-image"))

    def test_empty_content_returns_none(self):
        path = bottle_state.committed_image_path("dev")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("   \n")
        self.assertIsNone(bottle_state.read_committed_image("dev"))




class TestWhoStartedIt(unittest.TestCase):
    """`origin` and `agent_id` let a CLI resume bring a console bottle back as
    the same console agent."""

    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(use_bottle_root(root))

    def _write(self, origin: str = "", agent_id: str = "") -> None:
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity="b1", agent_name="demo", cwd="", copy_cwd=False, started_at="",
            origin=origin, agent_id=agent_id))

    def test_older_metadata_has_neither(self) -> None:
        self._write()
        meta = bottle_state.read_metadata("b1")
        assert meta is not None
        self.assertEqual(("", ""), (meta.origin, meta.agent_id))
        self.assertEqual("", bottle_state.console_agent_id("b1"))

    def test_only_a_console_bottle_names_a_console_agent(self) -> None:
        self._write(origin="console", agent_id="c-1")
        self.assertEqual("c-1", bottle_state.console_agent_id("b1"))
        self._write(origin="terminal", agent_id="c-1")
        self.assertEqual("", bottle_state.console_agent_id("b1"))
        self.assertEqual("", bottle_state.console_agent_id("missing"))

    def test_record_origin_updates_the_metadata(self) -> None:
        self._write()
        bottle_state.record_origin("b1", "console", "c-2")
        meta = bottle_state.read_metadata("b1")
        assert meta is not None
        self.assertEqual(("console", "c-2"), (meta.origin, meta.agent_id))
        self.assertEqual("demo", meta.agent_name)
        bottle_state.record_origin("missing", "console", "c-2")  # no metadata: no-op
        self.assertIsNone(bottle_state.read_metadata("missing"))


    def test_a_resume_rewrite_keeps_who_started_it(self) -> None:
        from types import SimpleNamespace
        from bot_bottle.backend.resolve_common import write_launch_metadata

        self._write(origin="console", agent_id="c-4")
        spec = SimpleNamespace(identity="b1", agent_name="demo", user_cwd="", copy_cwd=False,
                               label="b1", color="", bottle_names=("basic",))
        write_launch_metadata("b1", spec, compose_project="", backend="fake")  # type: ignore[arg-type]
        meta = bottle_state.read_metadata("b1")
        assert meta is not None
        self.assertEqual(("console", "c-4", "fake"), (meta.origin, meta.agent_id, meta.backend))
        # A fresh launch (no identity yet) starts with neither.
        fresh = SimpleNamespace(**{**vars(spec), "identity": ""})
        write_launch_metadata("b2", fresh, compose_project="", backend="fake")  # type: ignore[arg-type]
        meta = bottle_state.read_metadata("b2")
        assert meta is not None
        self.assertEqual(("", ""), (meta.origin, meta.agent_id))

if __name__ == "__main__":
    unittest.main()
