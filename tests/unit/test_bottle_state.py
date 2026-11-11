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


class TestRemoveBottledAgent(_FakeHomeMixin, unittest.TestCase):
    """Removing a bottled agent deletes its state dir, its snapshots' patches
    and its host DB rows together; nothing is inferred from directories."""

    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_removes_the_state_dir_and_the_rows(self):
        from bot_bottle.control.hostctl.sessions import SessionRecord, SessionStore
        from bot_bottle.control.hostctl.snapshots import snapshots_root
        from bot_bottle.paths import host_db_path
        write_metadata(BottleMetadata(identity="dev-x", agent_name="dev", cwd="",
                                      copy_cwd=False, started_at="t"))
        SessionStore(host_db_path()).put(SessionRecord("dev-x", "bottle", {}))
        SessionStore(host_db_path()).put(SessionRecord("other", "bottle", {}))
        (snapshots_root() / "dev-x" / "act").mkdir(parents=True)
        bottle_state.write_per_bottle_dockerfile("dev-x", "FROM x\n")
        d = bottle_state.bottle_state_dir("dev-x")
        self.assertTrue(bottle_state.remove_bottled_agent("dev-x"))
        self.assertFalse(d.exists())
        self.assertFalse((snapshots_root() / "dev-x").exists())
        self.assertIsNone(read_metadata("dev-x"))
        self.assertIsNone(SessionStore(host_db_path()).get("dev-x"))
        self.assertIsNotNone(SessionStore(host_db_path()).get("other"))

    def test_idempotent_when_nothing_is_there(self):
        self.assertFalse(bottle_state.remove_bottled_agent("never-existed"))

    def test_an_identity_that_isnt_one_path_part_is_refused(self):
        for bad in ("", ".", "..", "../x", "a/b", ".hidden"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                bottle_state.delete_bottled_agent(bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                BottleMetadata.from_json({"identity": bad, "agent_name": "a"})

    def test_a_state_dir_without_a_row_is_no_bottled_agent(self):
        bottle_state.write_per_bottle_dockerfile("stray", "FROM x\n")
        self.assertEqual([], bottle_state.all_metadata())


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
            compose_project="bot-bottle-dev-a4f8c",
            backend="firecracker",
            label="a label",
            bottle_names=("basic", "./main"),
            parent="p-1",
        )
        write_metadata(meta)
        self.assertEqual(meta, read_metadata("dev-a4f8c"))
        self.assertEqual(meta, BottleMetadata.from_json(meta.to_json()))

    def test_it_is_a_host_db_row_not_a_file(self):
        write_metadata(BottleMetadata(identity="dev-x", agent_name="dev", cwd="",
                                      copy_cwd=False, started_at="t"))
        self.assertFalse(bottle_state.bottle_state_dir("dev-x").exists())
        self.assertEqual(["dev-x"], [m.identity for m in bottle_state.all_metadata()])

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

    def test_only_some_fields_change(self):
        write_metadata(BottleMetadata(identity="dev-z", agent_name="dev", cwd="",
                                      copy_cwd=False, started_at="t"))
        with self.assertRaisesRegex(ValueError, "can't change agent_name"):
            bottle_state.update_metadata("dev-z", agent_name="other")
        self.assertIsNone(bottle_state.update_metadata("ghost", label="x"))


class TestTheCliGoesThroughTheHostController(_FakeHomeMixin, unittest.TestCase):
    """Outside the host controller, every read and write is a request to it:
    one writer for the host DB."""

    def setUp(self):
        from tests.unit import as_cli, serve_host_controller
        self._setup_fake_home()
        self.addCleanup(self._teardown_fake_home)
        serve_host_controller(self)
        as_cli(self)

    def test_write_read_change_and_remove(self):
        from unittest import mock
        from bot_bottle import host_access
        meta = BottleMetadata(identity="cli-1", agent_name="dev", cwd="/p", copy_cwd=True,
                              started_at="t", bottle_names=("basic",))
        with mock.patch.object(host_access, "ask", wraps=host_access.ask) as asked:
            write_metadata(meta)
            self.assertEqual(meta, read_metadata("cli-1"))
            changed = bottle_state.update_metadata("cli-1", label="  renamed ")
            assert changed is not None
            self.assertEqual("renamed", changed.label)
            bottle_state.write_per_bottle_dockerfile("cli-1", "FROM x\n")
            self.assertTrue(bottle_state.remove_bottled_agent("cli-1"))
        self.assertEqual(4, asked.call_count)
        self.assertEqual([], bottle_state.all_metadata())
        self.assertFalse(bottle_state.bottle_state_dir("cli-1").exists())

    def test_a_bad_label_is_refused_by_the_host_controller(self):
        from bot_bottle.control.hostctl.client import HostControlRefused
        write_metadata(BottleMetadata(identity="cli-2", agent_name="dev", cwd="",
                                      copy_cwd=False, started_at="t"))
        with self.assertRaises(HostControlRefused):
            bottle_state.update_metadata("cli-2", label="a\x00b")


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

    def test_a_bottle_not_started_yet_has_neither(self) -> None:
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
        bottle_state.record_origin("missing", "console", "c-2")  # no row: no-op
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
