"""Unit: `cli.py start --headless` non-interactive launch path.

Headless is the keystone for orchestrators, CI, and webhook
dispatch: agent/bottles/label come from flags + manifest defaults, no
TUI selectors fire, and the preflight y/N is auto-confirmed
(`assume_yes=True`). All actual launch work is stubbed so no container
is created.
"""

from __future__ import annotations

import io
import os
import unittest
from unittest.mock import MagicMock, patch

import bot_bottle.cli.commands.start as start_mod
import bot_bottle.cli.tui as tui_mod
from bot_bottle.backend import ActiveAgent
from bot_bottle.lib.log import Die
from bot_bottle.manifest import ManifestError


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
    manifest.require_agent = MagicMock(return_value=None)
    return manifest


def _active_agent(slug: str) -> ActiveAgent:
    return ActiveAgent(
        backend_name="docker",
        slug=slug,
        agent_name="demo",
        started_at="2026-01-01T00:00:00+00:00",
        services=(),
    )


class TestCmdStartHeadless(unittest.TestCase):
    """Drive `cmd_start --headless` with launch + TUI stubbed out."""

    def setUp(self):
        self._manifest = _make_manifest(
            ["researcher", "implementer"], ["claude", "dev"], agent_bottle="claude"
        )
        patch(
            "bot_bottle.cli.commands.start.ManifestIndex.resolve",
            return_value=self._manifest,
        ).start()
        self._launch_mock = patch(
            "bot_bottle.cli.commands.start._launch_bottle", return_value=0
        ).start()
        # No bottles running by default → no label collision.
        patch(
            "bot_bottle.cli.commands.start.enumerate_active_agents", return_value=[]
        ).start()
        # If any TUI picker fires in headless mode, that's a bug.
        self._agent_picker = patch.object(tui_mod, "filter_select").start()
        self._bottle_picker = patch.object(tui_mod, "filter_multiselect").start()
        self._modal = patch.object(tui_mod, "name_color_modal").start()
        patch.dict(os.environ, {}, clear=False).start()
        os.environ.pop("BOT_BOTTLE_BACKEND", None)
        # PTY check uses os.isatty(sys.stdin.fileno()); stub both so
        # headless unit tests aren't blocked on a real TTY.
        patch("bot_bottle.cli.commands.start.os.isatty", return_value=True).start()
        self.addCleanup(patch.stopall)

    def _spec(self):
        self._launch_mock.assert_called_once()
        return self._launch_mock.call_args[0][0]

    # -- no TUI in headless --------------------------------------------

    def test_headless_fires_no_pickers(self):
        rc = start_mod.cmd_start(
            ["--headless", "researcher", "--bottle", "claude", "--prompt", "Do it"]
        )
        self.assertEqual(0, rc)
        self._agent_picker.assert_not_called()
        self._bottle_picker.assert_not_called()
        self._modal.assert_not_called()

    def test_headless_assume_yes_forwarded(self):
        start_mod.cmd_start(
            ["--headless", "researcher", "--bottle", "claude", "--prompt", "Do it"]
        )
        self.assertTrue(self._launch_mock.call_args[1]["assume_yes"])

    # -- prompt --------------------------------------------------------

    def test_headless_without_prompt_dies(self):
        with self.assertRaises(Die):
            start_mod.cmd_start(["--headless", "researcher", "--bottle", "claude"])
        self._launch_mock.assert_not_called()

    def test_headless_prompt_forwarded_to_launch(self):
        start_mod.cmd_start(
            ["--headless", "researcher", "--bottle", "claude",
             "--prompt", "Implement issue #42"]
        )
        self.assertEqual(
            "Implement issue #42",
            self._launch_mock.call_args[1]["headless_prompt_text"],
        )

    # -- bottle resolution ---------------------------------------------

    def test_explicit_bottles_forwarded_in_order(self):
        start_mod.cmd_start(
            ["--headless", "researcher", "--bottle", "dev", "--bottle", "claude",
             "--prompt", "Do it"]
        )
        self.assertEqual(("dev", "claude"), self._spec().bottle_names)

    def test_omitted_bottle_falls_back_to_agent_default(self):
        start_mod.cmd_start(["--headless", "implementer", "--prompt", "Do it"])
        self.assertEqual(("claude",), self._spec().bottle_names)

    def test_no_bottle_and_no_default_dies(self):
        manifest = _make_manifest(["researcher"], ["claude"], agent_bottle="")
        with patch(
            "bot_bottle.cli.commands.start.ManifestIndex.resolve", return_value=manifest
        ):
            with self.assertRaises(Die):
                start_mod.cmd_start(
                    ["--headless", "researcher", "--prompt", "Do it"]
                )
        self._launch_mock.assert_not_called()

    # -- agent resolution ----------------------------------------------

    def test_missing_agent_name_dies(self):
        with self.assertRaises(Die):
            start_mod.cmd_start(["--headless"])
        self._launch_mock.assert_not_called()

    def test_unknown_agent_raises_manifest_error(self):
        self._manifest.require_agent.side_effect = ManifestError("agent 'x' not defined")
        with self.assertRaises(ManifestError):
            start_mod.cmd_start(
                ["--headless", "x", "--bottle", "claude", "--prompt", "Do it"]
            )
        self._launch_mock.assert_not_called()

    # -- label / color -------------------------------------------------

    def test_label_defaults_to_agent_name(self):
        start_mod.cmd_start(
            ["--headless", "researcher", "--bottle", "claude", "--prompt", "Do it"]
        )
        self.assertEqual("researcher", self._spec().label)

    def test_explicit_label_and_color_forwarded(self):
        start_mod.cmd_start(
            ["--headless", "researcher", "--bottle", "claude",
             "--label", "nightly", "--color", "green", "--prompt", "Do it"]
        )
        spec = self._spec()
        self.assertEqual("nightly", spec.label)
        self.assertEqual("green", spec.color)

    def test_label_collision_uniquifies(self):
        with patch(
            "bot_bottle.cli.commands.start.enumerate_active_agents",
            return_value=[_active_agent("researcher")],
        ):
            start_mod.cmd_start(
                ["--headless", "researcher", "--bottle", "claude", "--prompt", "Do it"]
            )
        self.assertEqual("researcher-2", self._spec().label)


    def test_cached_images_sets_cached_policy(self):
        start_mod.cmd_start(
            ["--headless", "--cached-images", "researcher", "--bottle", "claude",
             "--prompt", "Do it"]
        )
        self.assertEqual("cached", self._spec().image_policy)

    def test_cached_images_requires_headless(self):
        with self.assertRaises(Die):
            start_mod.cmd_start(["--cached-images", "researcher"])
        self._launch_mock.assert_not_called()


class TestPrepareWithPreflight(unittest.TestCase):
    """prepare_with_preflight calls render_preflight with the plan and backend name."""

    def test_render_preflight_called(self):
        from pathlib import Path
        mock_backend = MagicMock()
        mock_plan = MagicMock()
        mock_backend.prepare.return_value = mock_plan
        mock_backend.name = "test-backend"
        render = MagicMock()

        with patch("bot_bottle.cli.commands.start.get_bottle_backend", return_value=mock_backend), \
             patch("bot_bottle.cli.commands.start._identity_from_plan", return_value="id"), \
             patch("bot_bottle.cli.commands.start.info"):
            start_mod.prepare_with_preflight(
                MagicMock(),
                stage_dir=Path("/tmp"),
                render_preflight=render,
                prompt_yes=MagicMock(),
                dry_run=True,
            )

        render.assert_called_once_with(mock_plan, "test-backend")


class TestNoAgentsDefined(unittest.TestCase):
    """cmd_start prints an error and returns 1 when the manifest has no agents."""

    def test_no_agents_defined_returns_1(self):
        manifest = _make_manifest([], [])
        with patch(
            "bot_bottle.cli.commands.start.ManifestIndex.resolve", return_value=manifest
        ), patch("sys.stderr", io.StringIO()) as err:
            rc = start_mod.cmd_start([])
        self.assertEqual(1, rc)
        self.assertIn("no agents defined", err.getvalue())


class TestTextRenderPreflight(unittest.TestCase):
    """_text_render_preflight returns a renderer that prints the backend name."""

    def test_backend_name_in_output(self):
        render = start_mod._text_render_preflight()
        plan = MagicMock()
        with patch("bot_bottle.cli.commands.start._manifest_to_yaml", return_value=""), \
             patch("sys.stderr", io.StringIO()) as err:
            render(plan, "my-backend")
        self.assertIn("my-backend", err.getvalue())


if __name__ == "__main__":
    unittest.main()
