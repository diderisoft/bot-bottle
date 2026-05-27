"""Unit: dashboard's row-formatting + selection helpers (PRD 0019).

The active-bottle enumeration tests moved to
`test_docker_enumerate_active.py` (issue #77) — dashboard now
delegates listing to `enumerate_active_bottles` so the parser
and assembly tests live next to the docker backend's
implementation.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claude_bottle import supervise
from claude_bottle.cli import dashboard


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


class TestFormatAgentRow(unittest.TestCase):
    """One-line row formatting for the agents pane (PRD 0019 chunk 2)."""

    def _agent(self, **overrides) -> dashboard.ActiveAgent:
        defaults = dict(
            backend_name="docker",
            slug="dev-abc12",
            agent_name="implementer",
            started_at="2026-05-26T02:55:01+00:00",
            services=("egress", "git-gate", "pipelock", "supervise"),
        )
        defaults.update(overrides)
        return dashboard.ActiveAgent(**defaults)

    def test_renders_slug_name_time_services(self):
        s = dashboard._format_agent_row(self._agent(), 200)
        self.assertIn("dev-abc12", s)
        self.assertIn("implementer", s)
        self.assertIn("02:55:01", s)
        self.assertIn("egress,git-gate,pipelock,supervise", s)

    def test_starting_label_when_no_services(self):
        # Race window: compose project is up but containers haven't
        # been picked up by `docker ps` yet.
        s = dashboard._format_agent_row(self._agent(services=()), 200)
        self.assertIn("(starting)", s)

    def test_filters_agent_service_from_display(self):
        # The `agent` service is always present for an active bottle;
        # listing it is noise. The row should show only the sidecars.
        s = dashboard._format_agent_row(
            self._agent(services=("agent", "pipelock", "supervise")), 200,
        )
        self.assertIn("[pipelock,supervise]", s)
        self.assertNotIn("agent,", s)
        self.assertNotIn(",agent", s)

    def test_only_agent_service_shows_starting(self):
        # A bottle whose only running service is `agent` (sidecars
        # still warming up) renders as `(starting)`.
        s = dashboard._format_agent_row(self._agent(services=("agent",)), 200)
        self.assertIn("(starting)", s)

    def test_question_mark_when_no_started_at(self):
        s = dashboard._format_agent_row(self._agent(started_at=""), 200)
        self.assertIn("started ?", s)

    def test_truncates_to_maxw(self):
        s = dashboard._format_agent_row(self._agent(), 30)
        self.assertLessEqual(len(s), 30)
        self.assertTrue(s.endswith("…"))


class TestSelectionStatus(unittest.TestCase):
    """Idle-state status-line text for the agents-pane focus
    (PRD 0019 chunk 3). Empty when the proposals pane is focused;
    surfaces the selected agent (or a clear placeholder) when the
    agents pane is focused."""

    def _agent(self, slug: str) -> dashboard.ActiveAgent:
        return dashboard.ActiveAgent(
            backend_name="docker",
            slug=slug, agent_name="x", started_at="", services=(),
        )

    def test_empty_when_proposals_focused(self):
        s = dashboard._selection_status(
            dashboard.PANE_PROPOSALS, [self._agent("a-1")], 0,
        )
        self.assertEqual("", s)

    def test_no_agents_message_when_agents_pane_empty(self):
        s = dashboard._selection_status(dashboard.PANE_AGENTS, [], 0)
        self.assertEqual("[no active agents]", s)

    def test_shows_selected_slug(self):
        agents = [self._agent("a-1"), self._agent("b-2"), self._agent("c-3")]
        s = dashboard._selection_status(dashboard.PANE_AGENTS, agents, 1)
        self.assertEqual("[selected: b-2]", s)

    def test_out_of_bounds_falls_back_to_no_selection(self):
        agents = [self._agent("only")]
        s = dashboard._selection_status(dashboard.PANE_AGENTS, agents, 99)
        self.assertEqual("[no agent selected]", s)


class TestFilterAgents(unittest.TestCase):
    """Pure-function picker filter (PRD 0020 chunk 2). Curses-free
    so we can exercise the substring + case-insensitivity rules
    directly."""

    NAMES = ["implementer", "researcher", "triage-bot", "ImplDeluxe"]

    def test_empty_query_returns_all(self):
        self.assertEqual(self.NAMES, dashboard._filter_agents("", self.NAMES))

    def test_substring_match(self):
        self.assertEqual(
            ["implementer", "ImplDeluxe"],
            dashboard._filter_agents("impl", self.NAMES),
        )

    def test_case_insensitive(self):
        self.assertEqual(
            ["implementer", "ImplDeluxe"],
            dashboard._filter_agents("IMPL", self.NAMES),
        )

    def test_no_match_returns_empty(self):
        self.assertEqual([], dashboard._filter_agents("zzz", self.NAMES))

    def test_preserves_input_order(self):
        # Filtering should never re-sort; the picker draws in the
        # order the manifest exposed.
        out = dashboard._filter_agents("e", ["beta", "alpha", "echo"])
        self.assertEqual(["beta", "echo"], out)


class TestRunningCounts(unittest.TestCase):
    """Per-agent running-count surfaced in the picker so the
    operator sees `(N running)` before picking. Counts come from
    the dashboard's current `discover_active_agents` snapshot."""

    def _agent(self, agent_name: str) -> dashboard.ActiveAgent:
        return dashboard.ActiveAgent(
            backend_name="docker",
            slug=f"{agent_name}-abc",
            agent_name=agent_name,
            started_at="",
            services=(),
        )

    def test_empty_when_no_active_agents(self):
        self.assertEqual({}, dashboard._running_counts({}, []))

    def test_one_per_unique_agent_name(self):
        agents = [self._agent("a"), self._agent("b"), self._agent("c")]
        self.assertEqual(
            {"a": 1, "b": 1, "c": 1},
            dashboard._running_counts({}, agents),
        )

    def test_counts_collisions(self):
        agents = [
            self._agent("implementer"),
            self._agent("implementer"),
            self._agent("researcher"),
        ]
        self.assertEqual(
            {"implementer": 2, "researcher": 1},
            dashboard._running_counts({}, agents),
        )


class TestSelectedAgent(unittest.TestCase):
    """`_selected_agent` is what chunk 4's e/p key handlers use to
    decide whether to fire and which agent to target."""

    def _agent(self, slug: str, services: tuple[str, ...] = ()) -> dashboard.ActiveAgent:
        return dashboard.ActiveAgent(
            backend_name="docker",
            slug=slug, agent_name="x", started_at="", services=services,
        )

    def test_none_when_proposals_focused(self):
        agents = [self._agent("a-1")]
        self.assertIsNone(
            dashboard._selected_agent(dashboard.PANE_PROPOSALS, agents, 0),
        )

    def test_none_when_no_agents(self):
        self.assertIsNone(
            dashboard._selected_agent(dashboard.PANE_AGENTS, [], 0),
        )

    def test_returns_indexed_agent_when_in_range(self):
        agents = [self._agent("a-1"), self._agent("b-2")]
        result = dashboard._selected_agent(dashboard.PANE_AGENTS, agents, 1)
        self.assertIsNotNone(result)
        assert result is not None  # for type checker
        self.assertEqual("b-2", result.slug)

    def test_none_when_index_out_of_range(self):
        agents = [self._agent("only")]
        self.assertIsNone(
            dashboard._selected_agent(dashboard.PANE_AGENTS, agents, 99),
        )


class TestBottleForSlug(unittest.TestCase):
    """Re-attach target resolution (PRD 0020 chunk 3). Dashboard-
    owned bottles return the stored handle as-is; non-owned bottles
    get a synthesized DockerBottle backed by the slug-derived
    container name."""

    def test_owned_bottle_returns_held_handle(self):
        sentinel = object()
        bottles = {"dev-abc": (None, sentinel, "dev-abc")}
        bottle, _ = dashboard._bottle_for_slug("dev-abc", bottles, None)
        self.assertIs(sentinel, bottle)

    def test_unowned_synthesizes_docker_bottle(self):
        bottle, _ = dashboard._bottle_for_slug("dev-xyz", {}, None)
        # The synth wraps the slug-derived container name.
        self.assertEqual("claude-bottle-dev-xyz", bottle.name)

    def test_unowned_without_manifest_omits_prompt_path(self):
        bottle, hint = dashboard._bottle_for_slug("dev-xyz", {}, None)
        self.assertEqual("", hint)


class TestPickNextAfterStop(unittest.TestCase):
    """After `x` stops a bottle, the dashboard slides focus to
    the next agent — the one filling the stopped row, or the
    new last row if the stopped was last. Pure helper, easy
    to unit-test."""

    def _agent(self, slug: str) -> dashboard.ActiveAgent:
        return dashboard.ActiveAgent(
            backend_name="docker",
            slug=slug, agent_name=slug, started_at="", services=(),
        )

    def test_empty_list_returns_none(self):
        self.assertIsNone(
            dashboard._pick_next_after_stop([], 0, "anything"),
        )

    def test_only_agent_being_stopped_returns_none(self):
        # Stopping the last agent → nothing to focus.
        agents = [self._agent("only")]
        self.assertIsNone(
            dashboard._pick_next_after_stop(agents, 0, "only"),
        )

    def test_middle_row_slides_up_to_same_index(self):
        agents = [self._agent("a"), self._agent("b"), self._agent("c")]
        # Cursor was on "b" at index 1; stopping "b" → "c" now sits
        # at index 1 and takes focus.
        out = dashboard._pick_next_after_stop(agents, 1, "b")
        self.assertEqual((1, self._agent("c")), out)

    def test_last_row_wraps_to_new_last(self):
        agents = [self._agent("a"), self._agent("b"), self._agent("c")]
        # Cursor on "c" at index 2; stopping "c" leaves a 2-agent
        # list — index 2 is out of bounds, fall back to new last (1).
        out = dashboard._pick_next_after_stop(agents, 2, "c")
        self.assertEqual((1, self._agent("b")), out)

    def test_first_row(self):
        agents = [self._agent("a"), self._agent("b")]
        out = dashboard._pick_next_after_stop(agents, 0, "a")
        self.assertEqual((0, self._agent("b")), out)

    def test_clamps_negative_selection(self):
        # Defensive: a stale negative index doesn't crash.
        agents = [self._agent("a"), self._agent("b")]
        out = dashboard._pick_next_after_stop(agents, -1, "a")
        self.assertEqual((0, self._agent("b")), out)


class TestTmuxPaneArgvBuilders(unittest.TestCase):
    """Pure argv builders for the tmux split-pane integration
    (PRD 0021 chunk 2). The subprocess invocation itself is
    environment-dependent; here we lock the wrapping shape so
    a regression surfaces in CI without needing a real tmux."""

    DOCKER_ARGV = [
        "docker", "exec", "-it",
        "claude-bottle-dev-abc",
        "claude", "--dangerously-skip-permissions", "--continue",
    ]

    def test_split_pane_argv_horizontal_with_pane_id_capture(self):
        argv = dashboard._build_split_pane_argv(self.DOCKER_ARGV)
        self.assertEqual(
            ["tmux", "split-window", "-h",
             "-P", "-F", "#{pane_id}",
             *self.DOCKER_ARGV],
            argv,
        )

    def test_respawn_pane_argv_kills_existing_process(self):
        argv = dashboard._build_respawn_pane_argv("%12", self.DOCKER_ARGV)
        self.assertEqual(
            ["tmux", "respawn-pane", "-k", "-t", "%12", *self.DOCKER_ARGV],
            argv,
        )

    def test_respawn_pane_argv_threads_pane_id_unmodified(self):
        # Pane ids contain `%`; make sure we pass them straight
        # through to `-t` without quoting or substitution surprises.
        argv = dashboard._build_respawn_pane_argv("%abc.123", ["sh"])
        self.assertIn("%abc.123", argv)


class TestResumeArgvWithFallback(unittest.TestCase):
    """The `claude --continue || claude` shell fallback for the
    tmux re-attach path. Without it, an agent that's been spun
    up but never typed at crashes the pane on Enter because
    --continue has no session to resume."""

    def _bottle(self, prompt_path: str | None = None):
        from claude_bottle.backend.docker.bottle import DockerBottle
        return DockerBottle(
            container="claude-bottle-dev-abc",
            teardown=lambda: None,
            prompt_path_in_container=prompt_path,
        )

    def test_wraps_in_sh_c_with_or_fallback(self):
        argv = dashboard._build_resume_argv_with_fallback(self._bottle())
        # Must end with `sh -c '<cmd> --continue || <cmd>'`.
        self.assertEqual(
            ["docker", "exec", "-it", "claude-bottle-dev-abc", "sh", "-c"],
            argv[:6],
        )
        inner = argv[6]
        self.assertIn("--continue", inner)
        self.assertIn("||", inner)
        # Both branches mention claude.
        self.assertEqual(2, inner.count("claude"))

    def test_inner_args_quoted_safely(self):
        # Paths with spaces would break naive concatenation.
        bottle = self._bottle("/home/with space/.prompt")
        argv = dashboard._build_resume_argv_with_fallback(bottle)
        inner = argv[-1]
        # shlex.quote should single-quote any token with a space.
        self.assertIn("'/home/with space/.prompt'", inner)

    def test_includes_skip_permissions(self):
        argv = dashboard._build_resume_argv_with_fallback(self._bottle())
        self.assertIn("--dangerously-skip-permissions", argv[-1])

    def test_includes_prompt_file_flag_when_set(self):
        bottle = self._bottle("/home/node/.claude-bottle-prompt.txt")
        argv = dashboard._build_resume_argv_with_fallback(bottle)
        self.assertIn("--append-system-prompt-file", argv[-1])
        self.assertIn("/home/node/.claude-bottle-prompt.txt", argv[-1])


class TestClaudeRuntimeArgs(unittest.TestCase):
    """The argv passed to `bottle.claude_docker_argv` on each
    attach. Locked here so the tmux + foreground paths build
    identical claude invocations."""

    def test_default_skip_permissions_only(self):
        self.assertEqual(
            ["--dangerously-skip-permissions"],
            dashboard._claude_runtime_args(resume=False),
        )

    def test_resume_appends_continue(self):
        self.assertEqual(
            ["--dangerously-skip-permissions", "--continue"],
            dashboard._claude_runtime_args(resume=True),
        )

    def test_remote_control(self):
        args = dashboard._claude_runtime_args(
            resume=False, remote_control=True,
        )
        self.assertIn("--remote-control", args)


class TestStopBottleFlow(unittest.TestCase):
    """Explicit per-bottle stop (PRD 0020 chunk 4). The non-owned
    path is the one safe to test without curses + docker — the
    owned path drives `cm.__exit__` against a real launch context
    and belongs in integration tests."""

    def test_non_owned_returns_cleanup_hint(self):
        # stdscr is None here on purpose — the non-owned branch
        # returns before any curses call.
        msg = dashboard._stop_bottle_flow(
            stdscr=None,  # type: ignore[arg-type]
            bottles={},
            slug="ghost-zzz",
        )
        self.assertIn("not dashboard-owned", msg)
        self.assertIn("./cli.py cleanup", msg)

    def test_non_owned_does_not_touch_tmux_state(self):
        # PRD 0021: a stop on an unknown slug should never clear
        # the right-pane occupant tracking, even if the slugs
        # happen to match (defensive — non-owned can't be in the
        # right pane via the dashboard's normal flow anyway).
        tmux_state = {"pane_id": "%5", "slug": "live-bbb"}
        dashboard._stop_bottle_flow(
            stdscr=None,  # type: ignore[arg-type]
            bottles={},
            slug="ghost-zzz",
            tmux_state=tmux_state,
        )
        self.assertEqual({"pane_id": "%5", "slug": "live-bbb"}, tmux_state)


class TestOperatorEditFlowGuards(_FakeHomeMixin, unittest.TestCase):
    """Chunk-4 contract: the edit flow refuses when the selected
    agent doesn't have the required sidecar running. The discover-
    and-prompt scaffolding is gone, so the gating happens here
    instead of in the key handler."""

    def setUp(self) -> None:
        self._setup_fake_home()

    def tearDown(self) -> None:
        self._teardown_fake_home()

    def _agent(self, services: tuple[str, ...]) -> dashboard.ActiveAgent:
        return dashboard.ActiveAgent(
            backend_name="docker",
            slug="dev-abc12",
            agent_name="impl",
            started_at="",
            services=services,
        )

    def test_routes_edit_refuses_without_egress(self):
        # Bottle without bottle.egress.routes → no egress sidecar.
        msg = dashboard._operator_edit_flow(
            stdscr=None,  # type: ignore[arg-type]
            agent=self._agent(("pipelock", "supervise")),
            required_service="egress",
            label="routes",
            fetch=lambda _: "x",
            apply=lambda _slug, _content: None,
            suffix=".yaml",
        )
        self.assertIn("no running egress sidecar", msg)
        self.assertIn("dev-abc12", msg)

    def test_pipelock_edit_refuses_when_pipelock_missing(self):
        # Belt-and-braces — pipelock should always be there, but
        # the race window between `compose up` and `docker ps`
        # update is real.
        msg = dashboard._operator_edit_flow(
            stdscr=None,  # type: ignore[arg-type]
            agent=self._agent(()),
            required_service="pipelock",
            label="pipelock",
            fetch=lambda _: "x",
            apply=lambda _slug, _content: None,
            suffix=".txt",
        )
        self.assertIn("no running pipelock sidecar", msg)


if __name__ == "__main__":
    unittest.main()
