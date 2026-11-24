"""Unit: bare `bot-bottle` — which agent, what's copied in, `default`."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.unit import serve_host_controller
from bot_bottle.cli.commands import quick
from bot_bottle.contract.hostctl import LaunchRequest
from bot_bottle.lib.log import Die
from bot_bottle.manifest import ManifestError


def _manifest(agents: list[str]) -> MagicMock:
    manifest = MagicMock()
    manifest.all_agent_names = sorted(agents)

    def require_agent(name: str) -> None:
        if name not in agents:
            raise ManifestError(f"agent '{name}' not defined")

    manifest.require_agent.side_effect = require_agent
    return manifest


class Quick(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "root"
        self.project = Path(tmp.name) / "app"
        self.project.mkdir()
        env = patch.dict(os.environ, {"BOT_BOTTLE_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        # The remembered agents are the host controller's (`agent_prefs`).
        serve_host_controller(self)
        self.launched: list[LaunchRequest] = []

        def launch(request: LaunchRequest, *_args: object, **_kwargs: object) -> int:
            self.launched.append(request)
            return 0

        for target, effect in (("settings_from_env", MagicMock), ("_launch_bottle", launch)):
            p = patch(f"bot_bottle.cli.commands.quick.{target}", side_effect=effect)
            p.start()
            self.addCleanup(p.stop)

    def run_quick(self, argv: list[str], agents: list[str],
                  cwd: Path | None = None) -> LaunchRequest:
        with patch("bot_bottle.cli.commands.quick.ManifestIndex.resolve",
                   return_value=_manifest(agents)), \
                patch("bot_bottle.cli.commands.quick.Path.cwd", return_value=cwd or self.project), \
                patch("sys.stderr", io.StringIO()):
            self.assertEqual(0, quick.cmd_quick(argv))
        return self.launched[-1]

    def test_the_first_ready_starter_and_the_directory_copied(self) -> None:
        request = self.run_quick([], ["codex", "pi", "zeta"])
        # Agents don't name a bottle (ADR 0011): only `basic`, as nothing
        # else applies here.
        self.assertEqual(("codex", ("basic",), True, str(self.project),
                          "prefer-cached", True),
                         (request.agent, request.bottles, request.copy_cwd,
                          request.cwd, request.image_policy, request.assume_yes))

    def test_a_project_bottle_replaces_basic(self) -> None:
        bottles = self.project / ".bot-bottle" / "bottles"
        bottles.mkdir(parents=True)
        # Bottle is named after the directory ("app"), per PRD 0094 chunk 4.
        (bottles / "app.md").write_text("---\nextends: basic\n---\n")
        self.assertEqual(("./app",), self.run_quick([], ["pi"]).bottles)

    def test_a_legacy_main_bottle_still_works(self) -> None:
        bottles = self.project / ".bot-bottle" / "bottles"
        bottles.mkdir(parents=True)
        (bottles / "main.md").write_text("---\nextends: basic\n---\n")
        self.assertEqual(("./main",), self.run_quick([], ["pi"]).bottles)

    def test_a_terminal_launch_onboards_a_project_without_a_bottle(self) -> None:
        with patch("sys.stdin.isatty", return_value=True), \
                patch("bot_bottle.cli.commands.quick._onboard", return_value="./main") as onboard:
            self.assertEqual(("./main",), self.run_quick([], ["pi"]).bottles)
            onboard.assert_called_once_with(self.project)
            onboard.return_value = ""
            self.assertEqual(("basic",), self.run_quick([], ["pi"]).bottles)
            onboard.reset_mock()
            self.assertEqual(("basic",), self.run_quick(["--no-onboard"], ["pi"]).bottles)
            onboard.assert_not_called()

    def test_the_last_agent_used_here_wins_then_the_default(self) -> None:
        self.run_quick(["zeta"], ["codex", "zeta"])
        self.assertEqual("zeta", self.run_quick([], ["codex", "zeta"]).agent)
        other = self.project.parent / "other"
        other.mkdir()
        self.assertEqual("codex", self.run_quick([], ["codex", "zeta"], cwd=other).agent)
        with patch("bot_bottle.cli.commands.quick.ManifestIndex.resolve",
                   return_value=_manifest(["codex", "zeta"])), patch("sys.stderr", io.StringIO()):
            quick.cmd_default(["zeta"])
        third = self.project.parent / "third"
        third.mkdir()
        self.assertEqual("zeta", self.run_quick([], ["codex", "zeta"], cwd=third).agent)

    def test_one_remembered_agent_for_start_and_bare_bot_bottle(self) -> None:
        """`agent_prefs` keeps one remembered agent, in the host DB: what the
        guided start ran last is bare `bot-bottle`'s default, and a
        directory's own choice overrides it."""
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli import start_prefs
        start_prefs.remember_agent("zeta")
        self.assertEqual("zeta", quick.default_agent())
        self.assertEqual("zeta", self.run_quick([], ["codex", "zeta"]).agent)
        self.run_quick(["codex"], ["codex", "zeta"])
        self.assertEqual("codex", self.run_quick([], ["codex", "zeta"]).agent)
        self.assertEqual("zeta", start_prefs.remembered_agent())
        self.assertFalse((self.root / "start.json").exists())
        self.assertFalse((self.root / "state" / "quickstart.json").exists())

    def test_dry_run_is_a_flag_and_skips_onboarding(self) -> None:
        with patch("sys.stdin.isatty", return_value=True), \
                patch("bot_bottle.cli.commands.quick._onboard") as onboard, \
                patch("bot_bottle.cli.commands.quick._launch_bottle",
                      return_value=0) as launch, \
                patch("bot_bottle.cli.commands.quick.ManifestIndex.resolve",
                      return_value=_manifest(["pi"])), \
                patch("bot_bottle.cli.commands.quick.Path.cwd", return_value=self.project), \
                patch("sys.stderr", io.StringIO()):
            self.assertEqual(0, quick.cmd_quick(["--dry-run", "pi"]))
        onboard.assert_not_called()
        self.assertEqual("pi", launch.call_args.args[0].agent)
        self.assertTrue(launch.call_args.kwargs["dry_run"])

    def test_the_home_directory_is_not_shared(self) -> None:
        request = self.run_quick([], ["pi"], cwd=Path.home())
        self.assertFalse(request.copy_cwd)

    def test_an_unknown_agent_or_none_at_all_stops(self) -> None:
        for argv, agents in ((["nope"], ["pi"]), ([], [])):
            with self.subTest(argv=argv), patch(
                    "bot_bottle.cli.commands.quick.ManifestIndex.resolve",
                    return_value=_manifest(agents)), \
                    patch("sys.stderr", io.StringIO()), \
                    patch("sys.stdin.isatty", return_value=False), \
                    self.assertRaises(Die):
                quick.cmd_quick(argv)

    def test_with_no_agent_setup_runs_first_in_a_terminal(self) -> None:
        manifests = iter([_manifest([]), _manifest(["pi"])])

        def resolve(_cwd: str) -> MagicMock:
            return next(manifests)

        with patch("bot_bottle.cli.commands.quick.ManifestIndex.resolve", side_effect=resolve), \
                patch("bot_bottle.cli.commands.setup.cmd_setup", return_value=0) as setup, \
                patch("sys.stdin.isatty", return_value=True), patch("sys.stderr", io.StringIO()):
            quick.cmd_quick([])
        setup.assert_called_once_with(["--no-pair"])
        self.assertEqual("pi", self.launched[-1].agent)


class StartScreenTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "root"
        self.project = Path(tmp.name) / "app"
        self.project.mkdir()
        env = patch.dict(os.environ, {"BOT_BOTTLE_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        serve_host_controller(self)
        self.launched: list[LaunchRequest] = []

        def launch(request: LaunchRequest, *_args: object, **_kwargs: object) -> int:
            self.launched.append(request)
            return 0

        for target, effect in (("settings_from_env", MagicMock), ("_launch_bottle", launch)):
            p = patch(f"bot_bottle.cli.commands.quick.{target}", side_effect=effect)
            p.start()
            self.addCleanup(p.stop)

    def run_quick(self, argv, agents, cwd=None, capture_stderr=False):
        err = io.StringIO()
        with patch("bot_bottle.cli.commands.quick.ManifestIndex.resolve",
                   return_value=_manifest(agents)), \
                patch("bot_bottle.cli.commands.quick.Path.cwd",
                      return_value=cwd or self.project), \
                patch("sys.stderr", err):
            result = quick.cmd_quick(argv)
        if capture_stderr:
            return result, err.getvalue()
        return result

    def test_needs_start_screen_first_run(self) -> None:
        self.assertTrue(quick._needs_start_screen(self.project, _manifest(["pi"]),  # pylint: disable=protected-access
                                                   pick=False, remembered=False))

    def test_needs_start_screen_pick_forces_it(self) -> None:
        self.assertTrue(quick._needs_start_screen(self.project, _manifest(["pi"]),  # pylint: disable=protected-access
                                                   pick=True, remembered=True))

    def test_needs_start_screen_already_remembered(self) -> None:
        self.assertFalse(quick._needs_start_screen(self.project, _manifest(["pi"]),  # pylint: disable=protected-access
                                                    pick=False, remembered=True))

    def test_start_screen_shown_on_first_run_when_usable(self) -> None:
        with patch("bot_bottle.cli.commands.quick.screen") as sc, \
                patch("bot_bottle.cli.commands.quick._show_start_screen",
                      return_value="pi") as show:
            sc.usable.return_value = True
            self.run_quick([], ["pi"])
        show.assert_called_once()
        self.assertEqual("pi", self.launched[-1].agent)

    def test_start_screen_not_shown_when_terminal_not_usable(self) -> None:
        with patch("bot_bottle.cli.commands.quick.screen") as sc, \
                patch("bot_bottle.cli.commands.quick._show_start_screen") as show:
            sc.usable.return_value = False
            self.run_quick([], ["pi"])
        show.assert_not_called()

    def test_yes_flag_skips_start_screen(self) -> None:
        with patch("bot_bottle.cli.commands.quick.screen") as sc, \
                patch("bot_bottle.cli.commands.quick._show_start_screen") as show:
            sc.usable.return_value = True
            self.run_quick(["--yes"], ["pi"])
        show.assert_not_called()

    def test_pick_flag_forces_start_screen_on_remembered_agent(self) -> None:
        # Remember an agent first
        self.run_quick(["pi"], ["pi"])
        with patch("bot_bottle.cli.commands.quick.screen") as sc, \
                patch("bot_bottle.cli.commands.quick._show_start_screen",
                      return_value="pi") as show:
            sc.usable.return_value = True
            self.run_quick(["--pick"], ["pi"])
        show.assert_called_once()

    def test_explicit_agent_name_skips_start_screen(self) -> None:
        with patch("bot_bottle.cli.commands.quick.screen") as sc, \
                patch("bot_bottle.cli.commands.quick._show_start_screen") as show:
            sc.usable.return_value = True
            self.run_quick(["pi"], ["pi"])
        show.assert_not_called()

    def test_quit_from_start_screen_returns_zero(self) -> None:
        with patch("bot_bottle.cli.commands.quick.screen") as sc, \
                patch("bot_bottle.cli.commands.quick._show_start_screen",
                      return_value=None):
            sc.usable.return_value = True
            result = self.run_quick([], ["pi"])
        self.assertEqual(0, result)
        self.assertEqual([], self.launched)

    def test_start_screen_can_change_agent(self) -> None:
        with patch("bot_bottle.cli.commands.quick.screen") as sc, \
                patch("bot_bottle.cli.commands.quick._show_start_screen",
                      return_value="codex"):
            sc.usable.return_value = True
            self.run_quick([], ["pi", "codex"])
        self.assertEqual("codex", self.launched[-1].agent)

    def test_reminder_line_printed_after_first_launch(self) -> None:
        result, err = self.run_quick([], ["pi"], capture_stderr=True)
        self.assertEqual(0, result)
        self.assertIn("pi", err)
        self.assertIn("basic", err)
        self.assertIn(_short_path(self.project), err)
        self.assertIn("--pick to change", err)

    def test_reminder_line_uses_project_bottle_name(self) -> None:
        bottles = self.project / ".bot-bottle" / "bottles"
        bottles.mkdir(parents=True)
        (bottles / "app.md").write_text("---\nextends: basic\n---\n")
        result, err = self.run_quick([], ["pi"], capture_stderr=True)
        self.assertEqual(0, result)
        self.assertIn("app", err)
        self.assertNotIn("basic", err.split("--pick")[0])  # basic not in the reminder part

    def test_reminder_line_not_printed_for_explicit_agent(self) -> None:
        result, err = self.run_quick(["pi"], ["pi"], capture_stderr=True)
        self.assertEqual(0, result)
        self.assertNotIn("--pick to change", err)

    def test_reminder_line_not_printed_for_dry_run(self) -> None:
        result, err = self.run_quick(["--dry-run", "pi"], ["pi"], capture_stderr=True)
        self.assertNotIn("--pick to change", err)


def _short_path(cwd: Path) -> str:
    try:
        return "~/" + str(cwd.relative_to(Path.home()))
    except ValueError:
        return str(cwd)


if __name__ == "__main__":
    unittest.main()
