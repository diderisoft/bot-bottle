"""Unit: cmd_start selector dispatch (PRD 0051).

Tests that cmd_start calls filter_select only when the agent name is
absent, skips it when the agent is explicit, and returns 0 on cancel.

All actual launch work is stubbed so no container is created.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

import bot_bottle.cli.start as start_mod
import bot_bottle.cli.tui as tui_mod
from bot_bottle.backend import ActiveAgent


def _make_manifest(agent_names: list[str]):
    manifest = MagicMock()
    manifest.agents = {name: MagicMock() for name in agent_names}
    return manifest


class TestCmdStartSelector(unittest.TestCase):
    """Drive cmd_start with a minimal set of stubs."""

    def setUp(self):
        # Stub Manifest.resolve so no on-disk manifest is needed.
        self._manifest = _make_manifest(["researcher", "implementer"])
        self._resolve_patch = patch(
            "bot_bottle.cli.start.Manifest.resolve",
            return_value=self._manifest,
        )
        self._resolve_patch.start()

        # Stub _launch_bottle so no real container work happens.
        self._launch_patch = patch(
            "bot_bottle.cli.start._launch_bottle",
            return_value=0,
        )
        self._launch_mock = self._launch_patch.start()

        # Stub filter_select to avoid opening /dev/tty.
        self._tui_patch = patch.object(tui_mod, "filter_select")
        self._tui_mock = self._tui_patch.start()

        # Ensure BOT_BOTTLE_BACKEND is absent so omitted --backend
        # flows through to the resolver default.
        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop("BOT_BOTTLE_BACKEND", None)

    def tearDown(self):
        self._resolve_patch.stop()
        self._launch_patch.stop()
        self._tui_patch.stop()
        self._env_patch.stop()

    # ------------------------------------------------------------------
    # Both explicit — no picker shown
    # ------------------------------------------------------------------

    def test_both_explicit_skips_picker(self):
        self._tui_mock.return_value = "researcher"
        rc = start_mod.cmd_start(["--backend=docker", "researcher"])
        self.assertEqual(0, rc)
        self._tui_mock.assert_not_called()
        self._launch_mock.assert_called_once()
        _, kwargs = self._launch_mock.call_args
        self.assertEqual("docker", kwargs["backend_name"])

    # ------------------------------------------------------------------
    # Agent absent → agent picker fires; backend explicit
    # ------------------------------------------------------------------

    def test_agent_absent_shows_agent_picker(self):
        self._tui_mock.return_value = "researcher"
        rc = start_mod.cmd_start(["--backend=docker"])
        self.assertEqual(0, rc)
        self._tui_mock.assert_called_once()
        call_kwargs = self._tui_mock.call_args
        self.assertEqual(["implementer", "researcher"], call_kwargs[0][0])
        self.assertIn("agent", call_kwargs[1]["title"].lower())

    def test_agent_picker_cancel_returns_0(self):
        self._tui_mock.return_value = None
        rc = start_mod.cmd_start(["--backend=docker"])
        self.assertEqual(0, rc)
        self._launch_mock.assert_not_called()

    # ------------------------------------------------------------------
    # Agent explicit, backend absent → no picker
    # ------------------------------------------------------------------

    def test_backend_absent_uses_default_without_picker(self):
        rc = start_mod.cmd_start(["researcher"])
        self.assertEqual(0, rc)
        self._tui_mock.assert_not_called()
        self._launch_mock.assert_called_once()
        _, kwargs = self._launch_mock.call_args
        self.assertIsNone(kwargs["backend_name"])

    def test_bot_bottle_backend_env_skips_backend_picker(self):
        os.environ["BOT_BOTTLE_BACKEND"] = "docker"
        try:
            rc = start_mod.cmd_start(["researcher"])
        finally:
            os.environ.pop("BOT_BOTTLE_BACKEND", None)
        self.assertEqual(0, rc)
        self._tui_mock.assert_not_called()

    # ------------------------------------------------------------------
    # Both absent → only agent picker
    # ------------------------------------------------------------------

    def test_both_absent_shows_only_agent_picker(self):
        self._tui_mock.return_value = "researcher"
        rc = start_mod.cmd_start([])
        self.assertEqual(0, rc)
        self._tui_mock.assert_called_once()
        title = self._tui_mock.call_args[1]["title"].lower()
        self.assertIn("agent", title)
        self._launch_mock.assert_called_once()
        _, kwargs = self._launch_mock.call_args
        self.assertIsNone(kwargs["backend_name"])

    def test_both_absent_agent_cancel_skips_backend_picker(self):
        self._tui_mock.side_effect = [None]
        rc = start_mod.cmd_start([])
        self.assertEqual(0, rc)
        self.assertEqual(1, self._tui_mock.call_count)
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
        self._manifest = _make_manifest(["researcher"])
        patch("bot_bottle.cli.start.Manifest.resolve", return_value=self._manifest).start()
        self._launch_mock = patch(
            "bot_bottle.cli.start._launch_bottle", return_value=0,
        ).start()
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

        def _modal(default_label, *, disclaimer="", **_kw):
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
