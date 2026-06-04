"""Unit: cmd_start selector dispatch (PRD 0051).

Tests that cmd_start calls filter_select when name / backend are absent,
skips them when both are explicit, and returns 0 on cancel.

All actual launch work is stubbed so no container is created.
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch, call

import bot_bottle.cli.start as start_mod
import bot_bottle.cli.tui as tui_mod


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

        # Ensure BOT_BOTTLE_BACKEND is absent so the backend picker fires.
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
    # Agent explicit, backend absent → backend picker fires
    # ------------------------------------------------------------------

    def test_backend_absent_shows_backend_picker(self):
        self._tui_mock.return_value = "docker"
        rc = start_mod.cmd_start(["researcher"])
        self.assertEqual(0, rc)
        self._tui_mock.assert_called_once()
        call_kwargs = self._tui_mock.call_args
        self.assertIn("backend", call_kwargs[1]["title"].lower())

    def test_backend_picker_cancel_returns_0(self):
        self._tui_mock.return_value = None
        rc = start_mod.cmd_start(["researcher"])
        self.assertEqual(0, rc)
        self._launch_mock.assert_not_called()

    def test_bot_bottle_backend_env_skips_backend_picker(self):
        os.environ["BOT_BOTTLE_BACKEND"] = "docker"
        try:
            rc = start_mod.cmd_start(["researcher"])
        finally:
            os.environ.pop("BOT_BOTTLE_BACKEND", None)
        self.assertEqual(0, rc)
        self._tui_mock.assert_not_called()

    # ------------------------------------------------------------------
    # Both absent → agent picker then backend picker
    # ------------------------------------------------------------------

    def test_both_absent_shows_both_pickers_in_order(self):
        self._tui_mock.side_effect = ["researcher", "docker"]
        rc = start_mod.cmd_start([])
        self.assertEqual(0, rc)
        self.assertEqual(2, self._tui_mock.call_count)
        first_title = self._tui_mock.call_args_list[0][1]["title"].lower()
        second_title = self._tui_mock.call_args_list[1][1]["title"].lower()
        self.assertIn("agent", first_title)
        self.assertIn("backend", second_title)

    def test_both_absent_agent_cancel_skips_backend_picker(self):
        self._tui_mock.side_effect = [None]
        rc = start_mod.cmd_start([])
        self.assertEqual(0, rc)
        self.assertEqual(1, self._tui_mock.call_count)
        self._launch_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
