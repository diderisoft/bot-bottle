"""Unit: docker backend's `enumerate_active` (issue #77).

The full enumerate function fans out to `docker compose ls`,
`docker ps`, and per-bottle metadata.json reads — too much for a
unit test. Tests split into:

  - Parser tests for `_parse_services_by_project`: pure function,
    no I/O, deterministic on its input string.
  - Integration-shaped tests that monkeypatch the slug list +
    services map and read metadata from a fake home, then assert
    the assembled `ActiveAgent` shape.

The actual `docker ps` invocation is exercised by manual probing
during development and the (real-docker) integration tests; here
we lock down the shape contract so a regression surfaces in unit
CI. Tests moved out of `test_dashboard_active_agents.py` as part
of issue #77 — the dashboard now delegates to this layer.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot_bottle import supervise
from bot_bottle import bottle_state
from bot_bottle.backend.docker import enumerate as _enumerate


class TestParseServicesByProject(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual({}, _enumerate._parse_services_by_project(""))

    def test_one_container(self):
        out = _enumerate._parse_services_by_project(
            "bot-bottle-dev-abc\tegress\n"
        )
        self.assertEqual({"bot-bottle-dev-abc": {"egress"}}, out)

    def test_multiple_services_per_project(self):
        out = _enumerate._parse_services_by_project(
            "bot-bottle-dev-abc\tegress\n"
            "bot-bottle-dev-abc\tgit-gate\n"
            "bot-bottle-dev-abc\tsupervise\n"
        )
        self.assertEqual(
            {"bot-bottle-dev-abc": {"egress", "git-gate", "supervise"}},
            out,
        )

    def test_multiple_projects(self):
        out = _enumerate._parse_services_by_project(
            "proj-a\tegress\n"
            "proj-b\tgit-gate\n"
            "proj-a\tsupervise\n"
        )
        self.assertEqual(
            {"proj-a": {"egress", "supervise"}, "proj-b": {"git-gate"}},
            out,
        )

    def test_skips_lines_missing_either_field(self):
        # Defends against unlabeled containers slipping into the
        # output (the filter should prevent it, but be robust).
        out = _enumerate._parse_services_by_project(
            "bot-bottle-dev-abc\tegress\n"
            "no-tab-here\n"
            "\tmissing-project\n"
            "missing-service\t\n"
        )
        self.assertEqual({"bot-bottle-dev-abc": {"egress"}}, out)


class _FakeHomeMixin:
    def _setup_fake_home(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="enum-active.")
        original = supervise.bot_bottle_root

        def fake_root() -> Path:
            return Path(self._tmp.name) / ".bot-bottle"

        supervise.bot_bottle_root = fake_root  # type: ignore[assignment]
        self._restore_home = lambda: setattr(supervise, "bot_bottle_root", original)

    def _teardown_fake_home(self) -> None:
        self._restore_home()
        self._tmp.cleanup()


class TestEnumerateActive(_FakeHomeMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._setup_fake_home()
        self._orig_slugs = _enumerate.list_active_slugs
        self._orig_services = _enumerate._query_services_by_project

    def tearDown(self) -> None:
        _enumerate.list_active_slugs = self._orig_slugs
        _enumerate._query_services_by_project = self._orig_services
        self._teardown_fake_home()

    def _stub(self, slugs: list[str], services_by_project: dict[str, set[str]]) -> None:
        _enumerate.list_active_slugs = lambda **_: slugs  # type: ignore
        _enumerate._query_services_by_project = lambda: services_by_project

    def test_no_active_slugs_returns_empty(self):
        self._stub([], {})
        self.assertEqual([], _enumerate.enumerate_active())

    def test_assembles_from_metadata_and_services(self):
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity="dev-abc",
            agent_name="implementer",
            cwd="",
            copy_cwd=False,
            started_at="2026-05-26T03:00:00+00:00",
            compose_project="bot-bottle-dev-abc",
        ))
        self._stub(
            ["dev-abc"],
            {"bot-bottle-dev-abc": {"egress", "git-gate", "supervise"}},
        )
        active = _enumerate.enumerate_active()
        self.assertEqual(1, len(active))
        a = active[0]
        self.assertEqual("docker", a.backend_name)
        self.assertEqual("dev-abc", a.slug)
        self.assertEqual("implementer", a.agent_name)
        self.assertEqual("2026-05-26T03:00:00+00:00", a.started_at)
        self.assertEqual(("egress", "git-gate", "supervise"), a.services)

    def test_missing_metadata_renders_question_mark(self):
        # State dir doesn't exist for this slug — agent_name falls
        # back to "?" rather than dropping the row.
        self._stub(["mystery-zzz"], {"bot-bottle-mystery-zzz": {"egress"}})
        active = _enumerate.enumerate_active()
        self.assertEqual(1, len(active))
        self.assertEqual("?", active[0].agent_name)
        self.assertEqual("", active[0].started_at)
        self.assertEqual(("egress",), active[0].services)

    def test_no_services_for_project_yields_empty_tuple(self):
        # Race window between `compose up` returning and the actual
        # containers being listed in `docker ps` — render the row
        # but with no services.
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity="warming-up",
            agent_name="researcher",
            cwd="",
            copy_cwd=False,
            started_at="2026-05-26T03:05:00+00:00",
            compose_project="bot-bottle-warming-up",
        ))
        self._stub(["warming-up"], {})
        active = _enumerate.enumerate_active()
        self.assertEqual((), active[0].services)

    def test_preserves_slug_order(self):
        for slug in ("z-1", "a-1", "m-1"):
            bottle_state.write_metadata(bottle_state.BottleMetadata(
                identity=slug,
                agent_name=slug.split("-")[0],
                cwd="",
                copy_cwd=False,
                started_at="t",
                compose_project=f"bot-bottle-{slug}",
            ))
        # list_active_slugs returns sorted; preserve that order in
        # the output.
        self._stub(["a-1", "m-1", "z-1"], {})
        active = _enumerate.enumerate_active()
        self.assertEqual(
            ["a-1", "m-1", "z-1"],
            [a.slug for a in active],
        )


if __name__ == "__main__":
    unittest.main()
