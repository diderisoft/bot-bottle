"""The guided `bot-bottle start` (PRD 0117) and `--auto` (ADR 0018): the
remembered harness, the task, the suggestion, the confirm step, the edit
loop, and what gets launched (an unsaved suggestion runs as the run's own
bottle; nothing is written unless asked)."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests.unit import serve_host_controller
from bot_bottle.cli import start_prefs, suggest_client
from bot_bottle.cli.commands import guided, start
from bot_bottle.contract.hostctl import RUN_BOTTLE, LaunchRequest
from bot_bottle.suggest.proposal import (
    OPTION_REUSE,
    OPTION_STACK_ADDITIONS,
    Proposal,
    ProposalAdditions,
    ProposalEgress,
)


def _proposal(*hosts: str) -> Proposal:
    p = Proposal(option=OPTION_STACK_ADDITIONS if hosts else OPTION_REUSE, bottles=["basic"],
                 new_bottle=None,
                 additions=ProposalAdditions(egress=[ProposalEgress(h, ["GET"]) for h in hosts]),
                 similar=[], notes=[])
    p.source = "architect"
    return p


class _Term(io.StringIO):
    """A stream that is, or isn't, a terminal."""

    def __init__(self, tty: bool) -> None:
        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


class _Guided(unittest.TestCase):
    def setUp(self) -> None:
        # pylint: disable-next=consider-using-with
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.home / ".bot-bottle"
        for name in ("claude", "codex"):
            path = self.root / "agents" / f"{name}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"---\nagent_provider:\n  harness: {name}\n---\n\nHi.\n")
        (self.root / "bottles").mkdir()
        self.project = self.home / "project"
        self.project.mkdir()
        for patch in (mock.patch.dict(os.environ, {"HOME": str(self.home),
                                                   "BOT_BOTTLE_ROOT": str(self.root)}),
                      mock.patch.object(start.os, "getcwd", return_value=str(self.project)),
                      mock.patch.object(guided.os, "getcwd", return_value=str(self.project)),
                      mock.patch.object(suggest_client, "warm"),
                      mock.patch.object(guided, "logged_in_harnesses",
                                        return_value=["claude", "codex"])):
            patch.start()
            self.addCleanup(patch.stop)
        serve_host_controller(self)  # it remembers the agent (`agent_prefs`)
        self.launched: list[tuple[LaunchRequest, Any]] = []
        self.suggestion = _proposal("api.open-meteo.com")
        def answer(*_: object, **__: object) -> Proposal:
            return self.suggestion

        patcher = mock.patch.object(suggest_client, "suggest", side_effect=answer)
        self.suggest = patcher.start()
        self.addCleanup(patcher.stop)
        self.err = ""

    def run_start(self, argv: list[str], *, tty: bool = True, picks: list[str] | None = None,
                  lines: list[str] | None = None, env: dict[str, str] | None = None) -> int:
        def launch(request: LaunchRequest, index: Any, **_: Any) -> int:
            self.launched.append((request, index))
            return 0
        stdin, stdout = _Term(tty), _Term(tty)
        confirms = (guided.RUN, guided.SAVE, guided.EDIT, guided.CANCEL)
        harness = [p for p in picks or [] if p not in confirms]
        choices = [p for p in picks or [] if p in confirms]
        with mock.patch.object(start, "_launch_bottle", side_effect=launch), \
             mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", stdout), \
             mock.patch.object(guided.tui, "filter_select", side_effect=harness), \
             mock.patch.object(guided, "ask_choice", side_effect=choices), \
             mock.patch.object(guided, "read_tty_line", side_effect=list(lines or [])), \
             mock.patch.dict(os.environ, env or {}), \
             mock.patch("sys.stderr", io.StringIO()) as err:
            code = start.cmd_start(argv)
            self.err = err.getvalue()
        return code


class TestHarness(_Guided):
    def test_it_asks_once_then_remembers(self) -> None:
        self.run_start([], picks=["codex", guided.RUN], lines=["do it"])
        self.assertEqual("codex", start_prefs.remembered_agent())
        self.run_start([], picks=[guided.RUN], lines=["do it"])
        self.assertEqual(["codex", "codex"], [r.agent for r, _ in self.launched])

    def test_a_positional_agent_runs_and_becomes_the_default(self) -> None:
        start_prefs.remember_agent("codex")
        self.run_start(["claude"], picks=[guided.RUN], lines=["do it"])
        self.assertEqual("claude", self.launched[0][0].agent)
        self.assertEqual("claude", start_prefs.remembered_agent())

    def test_with_bottle_it_launches_as_before(self) -> None:
        with mock.patch.object(start, "_launch_bottle", return_value=0) as old, \
             mock.patch("sys.stderr", io.StringIO()):
            start.cmd_start(["claude", "--bottle", "basic"])
        self.assertEqual(("basic",), old.call_args.args[0].bottles)
        self.suggest.assert_not_called()


class TestTaskAndConfirm(_Guided):
    def setUp(self) -> None:
        super().setUp()
        start_prefs.remember_agent("claude")

    def test_an_empty_task_runs_in_basic_without_suggesting(self) -> None:
        self.run_start([], lines=[""])
        request = self.launched[0][0]
        self.assertEqual((("basic",), "", ""), (request.bottles, request.prompt,
                                                request.run_bottle))
        self.suggest.assert_not_called()

    def test_accepting_runs_an_unsaved_bottle_and_writes_nothing(self) -> None:
        self.run_start([], picks=[guided.RUN], lines=["build a weather cli"])
        request, index = self.launched[0]
        self.assertEqual(("basic", RUN_BOTTLE), request.bottles)
        self.assertEqual("build a weather cli", request.prompt)
        self.assertIn("api.open-meteo.com", request.run_bottle)
        self.assertEqual([], list((self.root / "bottles").iterdir()))
        self.assertTrue(any((d / f"{RUN_BOTTLE}.md").is_file() for d in index.extra_bottle_dirs))
        task, cwd = self.suggest.call_args.args
        self.assertEqual(("build a weather cli", self.project), (task, cwd))

    def test_a_reuse_needs_no_bottle_of_its_own(self) -> None:
        self.suggestion = _proposal()
        self.run_start([], picks=[guided.RUN], lines=["read the docs"])
        self.assertEqual((("basic",), ""), (self.launched[0][0].bottles,
                                            self.launched[0][0].run_bottle))

    def test_save_as_writes_a_named_bottle_and_runs_it(self) -> None:
        self.run_start([], picks=[guided.SAVE, guided.SAVE], lines=["build it", "Bad Name",
                                                                    "weather-cli"])
        request = self.launched[0][0]
        self.assertEqual((("basic", "weather-cli"), ""), (request.bottles, request.run_bottle))
        self.assertTrue((self.root / "bottles" / "weather-cli.md").is_file())
        self.assertIn("not a valid bottle name", self.err)

    def test_cancel_launches_nothing(self) -> None:
        self.assertEqual(0, self.run_start([], picks=[guided.CANCEL], lines=["x y"]))
        self.assertEqual([], self.launched)

    def test_edit_reopens_with_the_errors_until_it_validates(self) -> None:
        texts: list[str] = []
        answers = iter(["option: nonsense\n",
                        "option: stack+additions\nbottles:\n  - basic\nadditions:\n"
                        "  egress:\n    - host: api.example.com\n      methods: [GET]\n"])

        def editor(text: str) -> str:
            texts.append(text)
            return next(answers)

        with mock.patch.object(guided, "run_editor", side_effect=editor):
            self.run_start([], picks=[guided.EDIT, guided.RUN], lines=["do x"])
        self.assertEqual(2, len(texts))
        self.assertIn("api.open-meteo.com", texts[0])
        self.assertIn("# error: proposal.option must be one of", texts[1])
        self.assertIn("api.example.com", self.launched[0][0].run_bottle)
        self.assertNotIn("api.open-meteo.com", self.launched[0][0].run_bottle)

    def test_emptying_the_edit_keeps_the_proposal(self) -> None:
        with mock.patch.object(guided, "run_editor", return_value="# nothing\n"):
            self.run_start([], picks=[guided.EDIT, guided.RUN], lines=["do x"])
        self.assertIn("api.open-meteo.com", self.launched[0][0].run_bottle)


class TestAuto(_Guided):
    def setUp(self) -> None:
        super().setUp()
        start_prefs.remember_agent("claude")

    def test_auto_prints_one_line_and_launches_without_asking(self) -> None:
        self.run_start(["--auto"], lines=["build a weather cli"])
        self.assertIn("--auto: running in basic, adding egress api.open-meteo.com "
                      "(from architect)", self.err)
        self.assertEqual(("basic", RUN_BOTTLE), self.launched[0][0].bottles)
        self.assertEqual([], list((self.root / "bottles").iterdir()))

    def test_the_config_default_and_no_auto(self) -> None:
        self.run_start([], lines=["do x"], env={"BOT_BOTTLE_START_AUTO": "1"})
        self.assertEqual(1, len(self.launched))
        self.run_start(["--no-auto"], picks=[guided.CANCEL], lines=["do x"],
                       env={"BOT_BOTTLE_START_AUTO": "1"})
        self.assertEqual(1, len(self.launched))

    def test_without_a_terminal_auto_needs_the_task_and_never_prompts(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_start(["--auto"], tty=False)
        self.run_start(["--auto", "fix the flaky test"], tty=False)
        self.assertEqual("fix the flaky test", self.launched[0][0].prompt)

    def test_without_a_terminal_and_without_auto_nothing_is_suggested(self) -> None:
        with mock.patch.object(start, "_pick_bottles", return_value=None):
            self.run_start(["claude"], tty=False)
        self.suggest.assert_not_called()
        self.assertEqual([], self.launched)

    def test_auto_line_for_a_new_bottle(self) -> None:
        p = _proposal()
        p.bottles = []
        p.option = "new"
        p.source = ""
        p.new_bottle = mock.Mock(extends="")
        self.assertIn("running in basic (from rules)", guided.auto_line(p))


class TestPickAgent(unittest.TestCase):
    def test_without_a_terminal_it_never_asks(self) -> None:
        manifest = mock.Mock(all_agent_names=["claude", "codex"])
        with mock.patch.object(start_prefs, "remembered_agent", return_value=""), \
             mock.patch.object(guided, "logged_in_harnesses", return_value=["claude", "codex"]), \
             mock.patch.object(guided.tui, "filter_select") as pick:
            self.assertIsNone(guided.pick_agent(None, manifest, interactive=False))
        pick.assert_not_called()

    def test_logged_in_harnesses_need_an_agent_and_a_login(self) -> None:
        logged = mock.Mock(logged_in=True, installed=True)
        out = mock.Mock(logged_in=False, installed=False)
        providers = {"claude": logged, "codex": out}
        def provider(name: str) -> mock.Mock:
            return mock.Mock(detect_host=mock.Mock(return_value=providers[name]))

        with mock.patch("bot_bottle.agent_provider.get_provider", side_effect=provider):
            self.assertEqual(["claude"], guided.logged_in_harnesses(["claude", "codex"]))


if __name__ == "__main__":
    unittest.main()


class TestBlocked(_Guided):
    """A proposal that can't run here (`Proposal.blocked`) is never run."""

    def setUp(self) -> None:
        super().setUp()
        start_prefs.remember_agent("claude")
        self.suggestion.blocked = "claude-dev needs TOKEN_X, which isn't set on this host"

    def test_auto_refuses_it(self) -> None:
        self.assertEqual(1, self.run_start(["--auto"], lines=["do x"]))
        self.assertIn("--auto: the suggested bottle can't run here: claude-dev needs TOKEN_X",
                      self.err)
        self.assertEqual([], self.launched)

    def test_the_confirm_step_only_offers_edit_and_cancel(self) -> None:
        with mock.patch.object(guided, "ask_choice", return_value=guided.CANCEL) as ask:
            guided.confirm(self.suggestion, [])
        ask.assert_called_once_with(blocked=True)


class TestAskChoice(unittest.TestCase):
    """Step 4 is one keypress on a line under the proposal, so it stays on
    screen (no full-screen picker)."""

    def ask(self, keys: str, *, blocked: bool = False) -> tuple[str, str]:
        out = io.StringIO()
        with mock.patch("bot_bottle.cli.commands.supervise.read_key", side_effect=list(keys)), \
             mock.patch("sys.stdout", out):
            choice = guided.ask_choice(blocked=blocked)
        return choice, out.getvalue()

    def test_enter_runs_and_the_prompt_lists_the_keys(self) -> None:
        choice, shown = self.ask("\n")
        self.assertEqual(guided.RUN, choice)
        self.assertIn("[Enter] run  [s] save as…  [e] edit  [q] cancel", shown)
        self.assertNotIn("\x1b[?1049h", shown)  # never the alternate screen

    def test_each_key(self) -> None:
        for key, want in (("s", guided.SAVE), ("e", guided.EDIT), ("q", guided.CANCEL),
                          ("\x1b", guided.CANCEL), ("r", guided.RUN)):
            self.assertEqual(want, self.ask(key)[0])

    def test_other_keys_are_ignored(self) -> None:
        self.assertEqual(guided.EDIT, self.ask("xze")[0])

    def test_blocked_ignores_run_and_save(self) -> None:
        choice, shown = self.ask("\nse", blocked=True)
        self.assertEqual(guided.EDIT, choice)
        self.assertIn("It can't run here.", shown)
