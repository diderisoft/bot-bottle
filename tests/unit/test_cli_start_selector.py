"""Unit: cmd_start selector dispatch (PRD 0051, issue #269).

Tests that cmd_start calls filter_select only when the agent name is
absent, shows the bottle multiselect after agent selection, and skips
pickers when both are explicitly set.

All actual launch work is stubbed so no container is created.
"""

from __future__ import annotations

import os
import unittest
from collections.abc import Mapping, Sequence
from unittest.mock import MagicMock, patch

import bot_bottle.cli.commands.start as start_mod
import bot_bottle.cli.tui as tui_mod
from bot_bottle.manifest.preview import manifest_to_yaml
from bot_bottle.backend import ActiveAgent
from bot_bottle.contract.hostctl import LaunchRequest


def _make_manifest(
    agent_names: list[str],
    bottle_names: list[str] | None = None,
):
    manifest = MagicMock()
    manifest.agents = {name: MagicMock() for name in agent_names}
    manifest.all_agent_names = sorted(agent_names)
    manifest.all_bottle_names = sorted(bottle_names or [])
    manifest.home_md = None  # eager mode
    return manifest


class TestCmdStartSelector(unittest.TestCase):
    """Drive cmd_start with a minimal set of stubs."""

    def setUp(self):
        self._manifest = _make_manifest(["researcher", "implementer"], ["claude", "dev"])
        self._resolve_patch = patch(
            "bot_bottle.cli.commands.start.ManifestIndex.resolve",
            return_value=self._manifest,
        )
        self._resolve_patch.start()

        self._launch_patch = patch(
            "bot_bottle.cli.commands.start._launch_bottle",
            return_value=0,
        )
        self._launch_mock = self._launch_patch.start()

        # Stub filter_select (agent picker) and filter_multiselect (bottle picker).
        self._agent_picker_patch = patch.object(tui_mod, "filter_select")
        self._agent_picker_mock = self._agent_picker_patch.start()

        self._bottle_picker_patch = patch.object(tui_mod, "filter_multiselect")
        self._bottle_picker_mock = self._bottle_picker_patch.start()
        self._bottle_picker_mock.return_value = ["claude"]  # default: one bottle selected

        # name_color_modal opens /dev/tty and blocks on keyboard input on
        # self-hosted runners that have a real controlling terminal. Stub it
        # out like the other tui pickers so tests don't wait for a keypress.
        self._modal_patch = patch.object(
            tui_mod, "name_color_modal", return_value=("researcher", ""),
        )
        self._modal_patch.start()

        self._image_policy_patch = patch(
            "bot_bottle.cli.commands.start._select_image_policy",
            return_value="fresh",
        )
        self._image_policy_patch.start()

        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop("BOT_BOTTLE_BACKEND", None)

    def tearDown(self):
        self._resolve_patch.stop()
        self._launch_patch.stop()
        self._agent_picker_patch.stop()
        self._bottle_picker_patch.stop()
        self._modal_patch.stop()
        self._image_policy_patch.stop()
        self._env_patch.stop()

    # ------------------------------------------------------------------
    # Agent explicit — agent picker skipped; bottle picker always shown
    # ------------------------------------------------------------------

    def test_explicit_agent_skips_agent_picker(self):
        rc = start_mod.cmd_start(["researcher"])
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
        rc = start_mod.cmd_start([])
        self.assertEqual(0, rc)
        self._agent_picker_mock.assert_called_once()
        call_kwargs = self._agent_picker_mock.call_args
        self.assertEqual(["implementer", "researcher"], call_kwargs[0][0])
        self.assertIn("agent", call_kwargs[1]["title"].lower())
        # Bottle picker must also fire after agent selection.
        self._bottle_picker_mock.assert_called_once()

    def test_agent_picker_cancel_skips_bottle_picker(self):
        self._agent_picker_mock.return_value = None
        rc = start_mod.cmd_start([])
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
        self.assertEqual(("claude", "dev"), spec.bottles)

    def test_image_policy_forwarded_to_spec(self):
        with patch("bot_bottle.cli.commands.start._select_image_policy", return_value="cached"):
            start_mod.cmd_start(["--pick", "researcher"])
        self._launch_mock.assert_called_once()
        spec = self._launch_mock.call_args[0][0]
        self.assertEqual("cached", spec.image_policy)

    def test_image_policy_cancel_returns_0(self):
        with patch("bot_bottle.cli.commands.start._select_image_policy", return_value=None):
            rc = start_mod.cmd_start(["--pick", "researcher"])
        self.assertEqual(0, rc)
        self._launch_mock.assert_not_called()

    def test_empty_bottle_selection_forwarded(self):
        self._bottle_picker_mock.return_value = []
        start_mod.cmd_start(["researcher"])
        self._launch_mock.assert_called_once()
        spec = self._launch_mock.call_args[0][0]
        self.assertEqual((), spec.bottles)

    # ------------------------------------------------------------------
    # `basic` is preselected in the picker (ADR 0011)
    # ------------------------------------------------------------------

    def test_basic_prepopulates_bottle_picker(self):
        manifest = _make_manifest(["implementer"], ["basic", "claude", "dev"])
        with patch(
            "bot_bottle.cli.commands.start.ManifestIndex.resolve", return_value=manifest
        ):
            start_mod.cmd_start(["--pick", "implementer"])
        call_kwargs = self._bottle_picker_mock.call_args
        self.assertEqual(["basic"], call_kwargs[1]["initial"])

    def test_no_basic_empty_initial(self):
        manifest = _make_manifest(["researcher"], ["claude", "dev"])
        with patch(
            "bot_bottle.cli.commands.start.ManifestIndex.resolve", return_value=manifest
        ):
            start_mod.cmd_start(["researcher"])
        call_kwargs = self._bottle_picker_mock.call_args
        self.assertEqual([], call_kwargs[1]["initial"])

    # ------------------------------------------------------------------
    # Backend wiring
    # ------------------------------------------------------------------

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
        patch("bot_bottle.cli.commands.start.ManifestIndex.resolve", return_value=self._manifest).start()
        self._launch_mock = patch(
            "bot_bottle.cli.commands.start._launch_bottle", return_value=0,
        ).start()
        # Stub the bottle picker to always return a selection.
        patch.object(tui_mod, "filter_multiselect", return_value=["claude"]).start()
        patch("bot_bottle.cli.commands.start._select_image_policy", return_value="fresh").start()
        self.addCleanup(patch.stopall)

    def test_no_collision_proceeds_without_reprompt(self):
        with (
            patch.object(tui_mod, "name_color_modal", return_value=("researcher", "")) as modal,
            patch("bot_bottle.cli.commands.start.enumerate_active_agents", return_value=[]),
        ):
            rc = start_mod.cmd_start(["--pick", "researcher"])
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
                "bot_bottle.cli.commands.start.enumerate_active_agents",
                side_effect=[[collision_agent], []],
            ),
        ):
            rc = start_mod.cmd_start(["--pick", "researcher"])

        self.assertEqual(0, rc)
        self.assertEqual(2, modal.call_count)
        second_call_kwargs = modal.call_args_list[1][1]
        self.assertIn("researcher", second_call_kwargs.get("disclaimer", ""))
        self.assertIn("already in use", second_call_kwargs.get("disclaimer", ""))


class TestCmdStartDefaults(unittest.TestCase):
    """Without --pick, start asks nothing it can decide itself (PRD 0094)."""

    def setUp(self):
        self._launch_mock = patch(
            "bot_bottle.cli.commands.start._launch_bottle", return_value=0,
        ).start()
        self._bottles = patch.object(tui_mod, "filter_multiselect", return_value=["dev"]).start()
        self._modal = patch.object(tui_mod, "name_color_modal").start()
        self._policy = patch("bot_bottle.cli.commands.start._select_image_policy").start()
        self._active = patch("bot_bottle.cli.commands.start.enumerate_active_agents",
                             return_value=[]).start()
        self.addCleanup(patch.stopall)

    def start(self) -> LaunchRequest:
        manifest = _make_manifest(["pi"], ["pi", "dev"])
        with patch("bot_bottle.cli.commands.start.ManifestIndex.resolve", return_value=manifest):
            self.assertEqual(0, start_mod.cmd_start(["pi"]))
        return self._launch_mock.call_args[0][0]

    def test_the_label_is_deduplicated_against_running_bottles(self):
        self._active.return_value = [_active_agent("pi")]
        self.assertEqual("pi-2", self.start().label)

    def test_bottles_named_on_the_command_line_skip_the_picker(self):
        manifest = _make_manifest(["pi"], ["pi", "dev"])
        with patch("bot_bottle.cli.commands.start.ManifestIndex.resolve", return_value=manifest):
            self.assertEqual(0, start_mod.cmd_start(["pi", "--bottle", "dev", "--bottle", "pi"]))
        self._bottles.assert_not_called()
        self.assertEqual(("dev", "pi"), self._launch_mock.call_args[0][0].bottles)

    def test_only_the_bottle_picker_shows(self):
        request = self.start()
        self._bottles.assert_called_once()
        self._modal.assert_not_called()
        self._policy.assert_not_called()
        self.assertEqual((("dev",), "pi"), (request.bottles, request.label))


class TestBottleLineage(unittest.TestCase):
    """Unit tests for _bottle_lineage."""

    def test_returns_empty_in_eager_mode(self):
        manifest = _make_manifest(["agent"], ["base", "dev"])
        # home_md is None in eager mode → no file reads, returns {}
        result = start_mod._bottle_lineage(manifest)
        self.assertEqual({}, result)

    def test_reads_extends_chain_from_files(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            bottles_dir = Path(tmp) / "bottles"
            bottles_dir.mkdir()
            (bottles_dir / "base.md").write_text("---\n{}\n---\n")
            (bottles_dir / "mid.md").write_text("---\nextends: base\n---\n")
            (bottles_dir / "leaf.md").write_text("---\nextends: mid\n---\n")

            manifest = MagicMock()
            manifest.home_md = Path(tmp)

            result = start_mod._bottle_lineage(manifest)

        self.assertNotIn("base", result)          # no parent → not in map
        self.assertEqual("base -> mid", result["mid"])
        self.assertEqual("base -> mid -> leaf", result["leaf"])

    def test_cycle_protection(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            bottles_dir = Path(tmp) / "bottles"
            bottles_dir.mkdir()
            (bottles_dir / "a.md").write_text("---\nextends: b\n---\n")
            (bottles_dir / "b.md").write_text("---\nextends: a\n---\n")

            manifest = MagicMock()
            manifest.home_md = Path(tmp)

            result = start_mod._bottle_lineage(manifest)

        # Cycle must not hang; each should get a two-element chain.
        for name in ("a", "b"):
            self.assertIn(name, result)
            self.assertIn("->", result[name])


class TestManifestToYaml(unittest.TestCase):
    """Unit tests for manifest_to_yaml."""

    def _make_manifest_obj(
        self,
        *,
        skills: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        supervise: bool = True,
        harness: str = "claude",
    ):
        from bot_bottle.manifest import Manifest, ManifestBottle
        from bot_bottle.manifest.agent import ManifestAgent, ManifestAgentProvider

        agent = ManifestAgent(skills=tuple(skills),
                              agent_provider=ManifestAgentProvider(harness=harness))
        bottle = ManifestBottle(env=env or {}, supervise=supervise)
        return Manifest(agent=agent, bottle=bottle)

    def test_includes_agent_section(self):
        m = self._make_manifest_obj(skills=["researcher"])
        yaml = manifest_to_yaml(m)
        self.assertIn("agent:", yaml)
        self.assertIn("- researcher", yaml)

    def test_includes_bottle_section(self):
        m = self._make_manifest_obj(env={"FOO": "bar"})
        yaml = manifest_to_yaml(m)
        self.assertIn("bottle:", yaml)
        self.assertIn("FOO: bar", yaml)

    def test_supervise_rendered(self):
        m_true = self._make_manifest_obj(supervise=True)
        m_false = self._make_manifest_obj(supervise=False)
        self.assertIn("supervise: true", manifest_to_yaml(m_true))
        self.assertIn("supervise: false", manifest_to_yaml(m_false))

    def test_non_claude_provider_shown(self):
        m = self._make_manifest_obj(harness="codex")
        yaml = manifest_to_yaml(m)
        self.assertIn("agent_provider:", yaml)
        self.assertIn("harness: codex", yaml)

    def test_default_claude_provider_omitted(self):
        m = self._make_manifest_obj(harness="claude")
        yaml = manifest_to_yaml(m)
        self.assertNotIn("agent_provider:", yaml)


if __name__ == "__main__":
    unittest.main()
