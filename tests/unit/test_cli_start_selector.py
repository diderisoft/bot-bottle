"""Unit: cmd_start selector dispatch (PRD 0051, issue #269).

Tests that cmd_start calls filter_select only when the agent name is
absent, shows the bottle multiselect after agent selection, and skips
pickers when both are explicitly set.

All actual launch work is stubbed so no container is created.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

import bot_bottle.cli.start as start_mod
import bot_bottle.cli.tui as tui_mod
from bot_bottle.backend import ActiveAgent


def _make_manifest(
    agent_names: list[str],
    bottle_names: list[str] | None = None,
    agent_bottle: str = "",
):
    manifest = MagicMock()
    manifest.agents = {name: MagicMock(bottle=agent_bottle) for name in agent_names}
    manifest.all_agent_names = sorted(agent_names)
    manifest.all_bottle_names = sorted(bottle_names or [])
    manifest.home_md = None  # eager mode so _peek_agent_bottle uses agents dict
    return manifest


class TestCmdStartSelector(unittest.TestCase):
    """Drive cmd_start with a minimal set of stubs."""

    def setUp(self):
        self._manifest = _make_manifest(["researcher", "implementer"], ["claude", "dev"])
        self._resolve_patch = patch(
            "bot_bottle.cli.start.ManifestIndex.resolve",
            return_value=self._manifest,
        )
        self._resolve_patch.start()

        self._launch_patch = patch(
            "bot_bottle.cli.start._launch_bottle",
            return_value=0,
        )
        self._launch_mock = self._launch_patch.start()

        # Stub filter_select (agent picker) and filter_multiselect (bottle picker).
        self._agent_picker_patch = patch.object(tui_mod, "filter_select")
        self._agent_picker_mock = self._agent_picker_patch.start()

        self._bottle_picker_patch = patch.object(tui_mod, "filter_multiselect")
        self._bottle_picker_mock = self._bottle_picker_patch.start()
        self._bottle_picker_mock.return_value = ["claude"]  # default: one bottle selected

        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop("BOT_BOTTLE_BACKEND", None)

    def tearDown(self):
        self._resolve_patch.stop()
        self._launch_patch.stop()
        self._agent_picker_patch.stop()
        self._bottle_picker_patch.stop()
        self._env_patch.stop()

    # ------------------------------------------------------------------
    # Agent explicit — agent picker skipped; bottle picker always shown
    # ------------------------------------------------------------------

    def test_explicit_agent_skips_agent_picker(self):
        rc = start_mod.cmd_start(["--backend=docker", "researcher"])
        self.assertEqual(0, rc)
        self._agent_picker_mock.assert_not_called()
        self._bottle_picker_mock.assert_called_once()
        self._launch_mock.assert_called_once()

    def test_explicit_agent_bottle_picker_shows_available_bottles(self):
        start_mod.cmd_start(["researcher"])
        call_kwargs = self._bottle_picker_mock.call_args
        self.assertEqual(["claude", "dev"], call_kwargs[0][0])
        self.assertIn("bottle", call_kwargs[1]["title"].lower())

    # ------------------------------------------------------------------
    # Agent absent → agent picker fires; bottle picker always follows
    # ------------------------------------------------------------------

    def test_agent_absent_shows_agent_picker(self):
        self._agent_picker_mock.return_value = "researcher"
        rc = start_mod.cmd_start(["--backend=docker"])
        self.assertEqual(0, rc)
        self._agent_picker_mock.assert_called_once()
        call_kwargs = self._agent_picker_mock.call_args
        self.assertEqual(["implementer", "researcher"], call_kwargs[0][0])
        self.assertIn("agent", call_kwargs[1]["title"].lower())
        # Bottle picker must also fire after agent selection.
        self._bottle_picker_mock.assert_called_once()

    def test_agent_picker_cancel_skips_bottle_picker(self):
        self._agent_picker_mock.return_value = None
        rc = start_mod.cmd_start(["--backend=docker"])
        self.assertEqual(0, rc)
        self._bottle_picker_mock.assert_not_called()
        self._launch_mock.assert_not_called()

    def test_bottle_picker_cancel_returns_0(self):
        self._bottle_picker_mock.return_value = None
        rc = start_mod.cmd_start(["researcher"])
        self.assertEqual(0, rc)
        self._launch_mock.assert_not_called()

    # ------------------------------------------------------------------
    # Bottle selection is forwarded to BottleSpec
    # ------------------------------------------------------------------

    def test_selected_bottles_forwarded_to_spec(self):
        self._bottle_picker_mock.return_value = ["claude", "dev"]
        start_mod.cmd_start(["researcher"])
        self._launch_mock.assert_called_once()
        spec = self._launch_mock.call_args[0][0]
        self.assertEqual(("claude", "dev"), spec.bottle_names)

    def test_empty_bottle_selection_forwarded(self):
        self._bottle_picker_mock.return_value = []
        start_mod.cmd_start(["researcher"])
        self._launch_mock.assert_called_once()
        spec = self._launch_mock.call_args[0][0]
        self.assertEqual((), spec.bottle_names)

    # ------------------------------------------------------------------
    # Agent default bottle pre-populates the picker
    # ------------------------------------------------------------------

    def test_agent_bottle_prepopulates_bottle_picker(self):
        manifest = _make_manifest(
            ["implementer"], ["claude", "dev"], agent_bottle="claude"
        )
        with patch(
            "bot_bottle.cli.start.ManifestIndex.resolve", return_value=manifest
        ):
            start_mod.cmd_start(["implementer"])
        call_kwargs = self._bottle_picker_mock.call_args
        self.assertEqual(["claude"], call_kwargs[1]["initial"])

    def test_no_agent_bottle_empty_initial(self):
        manifest = _make_manifest(["researcher"], ["claude", "dev"], agent_bottle="")
        with patch(
            "bot_bottle.cli.start.ManifestIndex.resolve", return_value=manifest
        ):
            start_mod.cmd_start(["researcher"])
        call_kwargs = self._bottle_picker_mock.call_args
        self.assertEqual([], call_kwargs[1]["initial"])

    # ------------------------------------------------------------------
    # Backend wiring
    # ------------------------------------------------------------------

    def test_explicit_backend_forwarded(self):
        start_mod.cmd_start(["--backend=docker", "researcher"])
        _, kwargs = self._launch_mock.call_args
        self.assertEqual("docker", kwargs["backend_name"])

    def test_absent_backend_uses_default(self):
        start_mod.cmd_start(["researcher"])
        _, kwargs = self._launch_mock.call_args
        self.assertIsNone(kwargs["backend_name"])

    def test_bot_bottle_backend_env_skips_backend_picker(self):
        os.environ["BOT_BOTTLE_BACKEND"] = "docker"
        try:
            rc = start_mod.cmd_start(["researcher"])
        finally:
            os.environ.pop("BOT_BOTTLE_BACKEND", None)
        self.assertEqual(0, rc)

    def test_both_absent_shows_agent_picker_then_bottle_picker(self):
        self._agent_picker_mock.return_value = "researcher"
        rc = start_mod.cmd_start([])
        self.assertEqual(0, rc)
        self._agent_picker_mock.assert_called_once()
        self._bottle_picker_mock.assert_called_once()
        self._launch_mock.assert_called_once()

    def test_both_absent_agent_cancel_skips_bottle_and_launch(self):
        self._agent_picker_mock.return_value = None
        rc = start_mod.cmd_start([])
        self.assertEqual(0, rc)
        self._agent_picker_mock.assert_called_once()
        self._bottle_picker_mock.assert_not_called()
        self._launch_mock.assert_not_called()


def _active_agent(slug: str) -> ActiveAgent:
    return ActiveAgent(
        backend_name="docker",
        slug=slug,
        agent_name="demo",
        started_at="2026-01-01T00:00:00+00:00",
        services=(),
    )


class TestCmdStartLabelCollision(unittest.TestCase):
    """cmd_start re-prompts when the label's slug is already running."""

    def setUp(self):
        self._manifest = _make_manifest(["researcher"], ["claude"])
        patch("bot_bottle.cli.start.ManifestIndex.resolve", return_value=self._manifest).start()
        self._launch_mock = patch(
            "bot_bottle.cli.start._launch_bottle", return_value=0,
        ).start()
        # Stub the bottle picker to always return a selection.
        patch.object(tui_mod, "filter_multiselect", return_value=["claude"]).start()
        self.addCleanup(patch.stopall)

    def test_no_collision_proceeds_without_reprompt(self):
        with (
            patch.object(tui_mod, "name_color_modal", return_value=("researcher", "")) as modal,
            patch("bot_bottle.cli.start.enumerate_active_agents", return_value=[]),
        ):
            rc = start_mod.cmd_start(["researcher"])
        self.assertEqual(0, rc)
        modal.assert_called_once()
        self._launch_mock.assert_called_once()

    def test_collision_reprompts_with_disclaimer(self):
        collision_agent = _active_agent("researcher")
        call_count = 0

        def _modal(default_label: str, *, disclaimer: str = "", **_kw: object) -> tuple[str, str]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "researcher", ""
            return "researcher-2", ""

        with (
            patch.object(tui_mod, "name_color_modal", side_effect=_modal) as modal,
            patch(
                "bot_bottle.cli.start.enumerate_active_agents",
                side_effect=[[collision_agent], []],
            ),
        ):
            rc = start_mod.cmd_start(["researcher"])

        self.assertEqual(0, rc)
        self.assertEqual(2, modal.call_count)
        second_call_kwargs = modal.call_args_list[1][1]
        self.assertIn("researcher", second_call_kwargs.get("disclaimer", ""))
        self.assertIn("already in use", second_call_kwargs.get("disclaimer", ""))


if __name__ == "__main__":
    unittest.main()
