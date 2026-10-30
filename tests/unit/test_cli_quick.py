"""Unit: bare `bot-bottle` — which agent, what's copied in, `default`."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.cli.commands import quick
from bot_bottle.contract.hostctl import LaunchRequest
from bot_bottle.lib.log import Die


def _manifest(agents: list[str]) -> MagicMock:
    manifest = MagicMock()
    manifest.all_agent_names = sorted(agents)
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
        self.launched: list[LaunchRequest] = []

        def second(_first: object, value: str) -> str:
            return value  # an agent's bottle is its own name; a label is free

        def launch(request: LaunchRequest, *_args: object, **_kwargs: object) -> int:
            self.launched.append(request)
            return 0

        for target, effect in (("_peek_agent_bottle", second), ("_uniquify_label", second),
                               ("settings_from_env", MagicMock), ("_launch_bottle", launch)):
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
        self.assertEqual(("codex", ("codex", "basic"), True, str(self.project),
                          "prefer-cached", True),
                         (request.agent, request.bottles, request.copy_cwd,
                          request.cwd, request.image_policy, request.assume_yes))

    def test_a_project_bottle_replaces_basic(self) -> None:
        bottles = self.project / ".bot-bottle" / "bottles"
        bottles.mkdir(parents=True)
        (bottles / "main.md").write_text("---\nextends: basic\n---\n")
        self.assertEqual(("pi", "./main"), self.run_quick([], ["pi"]).bottles)

    def test_a_terminal_launch_onboards_a_project_without_a_bottle(self) -> None:
        with patch("sys.stdin.isatty", return_value=True), \
                patch("bot_bottle.cli.commands.quick._onboard", return_value="./main") as onboard:
            self.assertEqual(("pi", "./main"), self.run_quick([], ["pi"]).bottles)
            onboard.assert_called_once_with(self.project)
            onboard.return_value = ""
            self.assertEqual(("pi", "basic"), self.run_quick([], ["pi"]).bottles)
            onboard.reset_mock()
            self.assertEqual(("pi", "basic"), self.run_quick(["--no-onboard"], ["pi"]).bottles)
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


if __name__ == "__main__":
    unittest.main()
