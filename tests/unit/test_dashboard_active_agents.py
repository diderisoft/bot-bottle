"""Unit: dashboard.discover_active_agents (PRD 0019 chunk 1).

The full discover function fans out to `docker compose ls`, `docker
ps`, and per-bottle metadata.json reads — too much for a unit test.
Tests split into:

  - Parser tests for `_parse_services_by_project`: pure function, no
    I/O, deterministic on its input string.
  - Integration-shaped tests that monkeypatch the slug list +
    services map and read metadata from a fake home, then assert
    the assembled `ActiveAgent` shape.

The actual `docker ps` invocation is exercised by manual probing
during development and the (real-docker) integration tests; here
we lock down the shape contract so a regression surfaces in unit CI.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claude_bottle import supervise
from claude_bottle.backend.docker import bottle_state
from claude_bottle.cli import dashboard


class TestParseServicesByProject(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual({}, dashboard._parse_services_by_project(""))

    def test_one_container(self):
        out = dashboard._parse_services_by_project(
            "claude-bottle-dev-abc\tegress\n"
        )
        self.assertEqual({"claude-bottle-dev-abc": {"egress"}}, out)

    def test_multiple_services_per_project(self):
        out = dashboard._parse_services_by_project(
            "claude-bottle-dev-abc\tegress\n"
            "claude-bottle-dev-abc\tpipelock\n"
            "claude-bottle-dev-abc\tsupervise\n"
        )
        self.assertEqual(
            {"claude-bottle-dev-abc": {"egress", "pipelock", "supervise"}},
            out,
        )

    def test_multiple_projects(self):
        out = dashboard._parse_services_by_project(
            "proj-a\tegress\n"
            "proj-b\tpipelock\n"
            "proj-a\tsupervise\n"
        )
        self.assertEqual(
            {"proj-a": {"egress", "supervise"}, "proj-b": {"pipelock"}},
            out,
        )

    def test_skips_lines_missing_either_field(self):
        # Defends against unlabeled containers slipping into the
        # output (the filter should prevent it, but be robust).
        out = dashboard._parse_services_by_project(
            "claude-bottle-dev-abc\tegress\n"
            "no-tab-here\n"
            "\tmissing-project\n"
            "missing-service\t\n"
        )
        self.assertEqual({"claude-bottle-dev-abc": {"egress"}}, out)


class _FakeHomeMixin:
    def _setup_fake_home(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dashboard-aa-test.")
        original = supervise.claude_bottle_root

        def fake_root() -> Path:
            return Path(self._tmp.name) / ".claude-bottle"

        supervise.claude_bottle_root = fake_root  # type: ignore[assignment]
        self._restore_home = lambda: setattr(supervise, "claude_bottle_root", original)

    def _teardown_fake_home(self) -> None:
        self._restore_home()
        self._tmp.cleanup()


class TestDiscoverActiveAgents(_FakeHomeMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._setup_fake_home()
        self._orig_slugs = dashboard.list_active_slugs
        self._orig_services = dashboard._query_services_by_project

    def tearDown(self) -> None:
        dashboard.list_active_slugs = self._orig_slugs
        dashboard._query_services_by_project = self._orig_services
        self._teardown_fake_home()

    def _stub(self, slugs: list[str], services_by_project: dict[str, set[str]]) -> None:
        dashboard.list_active_slugs = lambda: slugs
        dashboard._query_services_by_project = lambda: services_by_project

    def test_no_active_slugs_returns_empty(self):
        self._stub([], {})
        self.assertEqual([], dashboard.discover_active_agents())

    def test_assembles_from_metadata_and_services(self):
        bottle_state.write_metadata(bottle_state.BottleMetadata(
            identity="dev-abc",
            agent_name="implementer",
            cwd="",
            copy_cwd=False,
            started_at="2026-05-26T03:00:00+00:00",
            compose_project="claude-bottle-dev-abc",
        ))
        self._stub(
            ["dev-abc"],
            {"claude-bottle-dev-abc": {"pipelock", "egress", "supervise"}},
        )
        agents = dashboard.discover_active_agents()
        self.assertEqual(1, len(agents))
        a = agents[0]
        self.assertEqual("dev-abc", a.slug)
        self.assertEqual("implementer", a.agent_name)
        self.assertEqual("2026-05-26T03:00:00+00:00", a.started_at)
        self.assertEqual(("egress", "pipelock", "supervise"), a.services)

    def test_missing_metadata_renders_question_mark(self):
        # State dir doesn't exist for this slug — agent_name falls
        # back to "?" rather than dropping the row.
        self._stub(["mystery-zzz"], {"claude-bottle-mystery-zzz": {"pipelock"}})
        agents = dashboard.discover_active_agents()
        self.assertEqual(1, len(agents))
        self.assertEqual("?", agents[0].agent_name)
        self.assertEqual("", agents[0].started_at)
        self.assertEqual(("pipelock",), agents[0].services)

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
            compose_project="claude-bottle-warming-up",
        ))
        self._stub(["warming-up"], {})
        agents = dashboard.discover_active_agents()
        self.assertEqual((), agents[0].services)

    def test_preserves_slug_order(self):
        for slug in ("z-1", "a-1", "m-1"):
            bottle_state.write_metadata(bottle_state.BottleMetadata(
                identity=slug,
                agent_name=slug.split("-")[0],
                cwd="",
                copy_cwd=False,
                started_at="t",
                compose_project=f"claude-bottle-{slug}",
            ))
        # list_active_slugs returns sorted; preserve that order in
        # the output.
        self._stub(["a-1", "m-1", "z-1"], {})
        agents = dashboard.discover_active_agents()
        self.assertEqual(
            ["a-1", "m-1", "z-1"],
            [a.slug for a in agents],
        )


if __name__ == "__main__":
    unittest.main()
