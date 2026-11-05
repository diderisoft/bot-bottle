"""The guided start on the shared view: the steps as keys move through them,
the review's keys, setup offered when no agent exists, and the launch
handed the view and the finished steps."""

from __future__ import annotations

import argparse
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.cli import screen, start_prefs, suggest_client
from bot_bottle.cli.commands import guided, guided_view, start
from bot_bottle.cli.screen import DONE, Screen
from bot_bottle.contract.hostctl import RUN_BOTTLE, LaunchRequest, Settings
from bot_bottle.manifest import ManifestIndex
from bot_bottle.suggest.proposal import (
    OPTION_STACK_ADDITIONS, Proposal, ProposalAdditions, ProposalEgress,
)


def _proposal(*hosts: str, notes: list[str] | None = None) -> Proposal:
    p = Proposal(option=OPTION_STACK_ADDITIONS, bottles=["basic"], new_bottle=None,
                 additions=ProposalAdditions(egress=[ProposalEgress(h, ["GET"], why="the API")
                                                     for h in hosts]),
                 similar=[], notes=notes or [])
    p.source = "architect"
    return p


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _args(**kw: Any) -> argparse.Namespace:
    base = {"name": None, "prompt": None, "label": None, "color": None, "cwd": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _ready(agents: list[str]) -> list[str]:
    return [a for a in agents if a != "bottle-architect"]


def _booting(flow: guided_view.StartView) -> None:
    flow.warm_state = "booting"


class _View(unittest.TestCase):
    def setUp(self) -> None:
        # pylint: disable-next=consider-using-with
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.home / ".bot-bottle"
        (self.root / "agents").mkdir(parents=True)
        (self.root / "bottles").mkdir()
        self.project = self.home / "project"
        self.project.mkdir()
        self.agents("claude", "codex")
        for patch in (mock.patch.dict(os.environ, {"HOME": str(self.home),
                                                   "BOT_BOTTLE_ROOT": str(self.root)}),
                      mock.patch.object(screen, "size", return_value=(120, 40)),
                      mock.patch.object(guided, "logged_in_harnesses", side_effect=_ready),
                      mock.patch.object(guided_view, "origin_of", return_value="")):
            patch.start()
            self.addCleanup(patch.stop)
        self.warm = self.enterContext(mock.patch.object(suggest_client, "warm",
                                                        return_value="ready"))
        self.suggestion = _proposal("api.open-meteo.com", notes=["no key needed"])
        self.suggest = self.enterContext(mock.patch.object(
            suggest_client, "suggest", side_effect=self.answer))
        self.launched: list[tuple[LaunchRequest, Any, dict[str, Any]]] = []
        self.out = _Tty()
        self.view = Screen(self.out, color=False)

    def answer(self, *_: Any, **__: Any) -> Proposal:
        return self.suggestion

    def agents(self, *names: str) -> None:
        for name in names:
            (self.root / "agents" / f"{name}.md").write_text(
                f"---\nagent_provider:\n  harness: {name}\n---\n\nHi.\n")

    def run_view(self, keys: list[str], **args: Any) -> int:
        feed = iter(keys)

        def key(_timeout: float | None = None) -> str | None:
            try:
                value = next(feed)
            except StopIteration as e:
                raise AssertionError("the flow wanted more keys") from e
            if value == "^C":
                raise KeyboardInterrupt
            return value

        def launch(request: LaunchRequest, index: Any, **kw: Any) -> int:
            self.launched.append((request, index, kw))
            return 0

        manifest = ManifestIndex.resolve(str(self.project))
        return guided_view.StartView(self.view, _args(**args), manifest, Settings(),
                                     launch=launch, keys=key, cwd=self.project).run()

    def frames(self) -> str:
        return screen.strip(self.out.getvalue())


class TestSteps(_View):
    def test_pick_type_review_run(self) -> None:
        code = self.run_view(["down", "enter", *"fix it", "enter", "enter"])
        self.assertEqual(0, code)
        request, _index, kw = self.launched[0]
        self.assertEqual("codex", request.agent)
        self.assertEqual("fix it", request.prompt)
        self.assertEqual(RUN_BOTTLE, request.bottles[-1])
        self.assertIn("api.open-meteo.com", request.run_bottle)
        self.assertIs(self.view, kw["view"])
        self.assertEqual(["Project", "Agent", "Task", "Bottle"], [s.title for s in kw["lead"]])
        self.assertTrue(all(s.state == DONE for s in kw["lead"]))
        self.assertEqual("codex", start_prefs.remembered_agent())
        self.assertIn("Claude Code · logged in", self.frames())

    def test_a_remembered_agent_skips_the_menu(self) -> None:
        start_prefs.remember_agent("claude")
        self.run_view([*"go", "enter", "enter"])
        self.assertEqual("claude", self.launched[0][0].agent)

    def test_an_empty_task_opens_the_agent_in_basic_without_suggesting(self) -> None:
        self.run_view(["enter", "enter"], name="claude")
        request = self.launched[0][0]
        self.assertEqual(("basic",), request.bottles)
        self.assertEqual("", request.prompt)
        self.suggest.assert_not_called()

    def test_backspace_edits_the_task(self) -> None:
        self.run_view([*"fixx", "backspace", "enter", "enter"], name="claude")
        self.assertEqual("fix", self.launched[0][0].prompt)

    def test_the_task_from_args_skips_its_step(self) -> None:
        self.run_view(["enter"], name="claude", prompt="fix it")
        self.assertEqual("fix it", self.launched[0][0].prompt)


class TestReview(_View):
    def test_cancel_launches_nothing_and_writes_nothing(self) -> None:
        self.assertEqual(0, self.run_view(["q"], name="claude", prompt="t"))
        self.assertEqual([], self.launched)
        self.assertEqual(["claude.md", "codex.md"],
                         sorted(p.name for p in (self.root / "agents").iterdir()))
        self.assertFalse(any((self.root / "bottles").iterdir()))
        self.assertFalse(self.view.on)

    def test_the_review_is_compact_until_details(self) -> None:
        self.run_view(["d", "enter"], name="claude", prompt="t")
        text = self.frames()
        self.assertIn("+ egress api.open-meteo.com", text)
        self.assertIn("1 note · d to read", text)
        self.assertIn("note: no key needed", text)  # after `d`
        self.assertIn(guided_view.REVIEW_KEYS, text)

    def test_save_as_writes_the_bottle_and_runs_it(self) -> None:
        with mock.patch("bot_bottle.suggest.write.write_bottle") as write:
            self.run_view(["s", *"weather", "enter"], name="claude", prompt="t")
        self.assertEqual("weather", write.call_args.args[0])
        request = self.launched[0][0]
        self.assertIn("weather", request.bottles)
        self.assertEqual("", request.run_bottle)

    def test_edit_comes_back_to_the_review(self) -> None:
        edited = _proposal("api.weather.gov")
        with mock.patch.object(guided, "edit_proposal", return_value=edited):
            self.run_view(["e", "enter"], name="claude", prompt="t")
        self.assertIn("api.weather.gov", self.launched[0][0].run_bottle)

    def test_a_blocked_proposal_only_edits_or_cancels(self) -> None:
        self.suggestion.blocked = "claude-dev needs X"
        self.run_view(["enter", "s", "q"], name="claude", prompt="t")
        self.assertEqual([], self.launched)
        self.assertIn("can't run: claude-dev needs X", self.frames())
        self.assertIn(guided_view.BLOCKED_KEYS, self.frames())


class TestSuggesting(_View):
    def test_a_cold_architect_says_it_is_warming(self) -> None:
        self.warm.return_value = "booting"
        gate = threading.Event()

        def slow(*_: Any, **__: Any) -> Proposal:
            gate.wait(2)
            return self.suggestion

        self.suggest.side_effect = slow
        original = guided_view.StartView.draw

        def draw(flow: guided_view.StartView, footer: str = "") -> None:
            original(flow, footer)
            if "warming the architect" in screen.strip(self.out.getvalue()):
                gate.set()

        with mock.patch.object(guided_view.StartView, "draw", draw), \
             mock.patch.object(guided_view.StartView, "_warm", _booting):
            self.run_view(["enter"], name="claude", prompt="t")
        self.assertIn("warming the architect…", self.frames())


class TestLongTask(_View):
    def test_a_long_task_wraps_and_keeps_the_bottle_still(self) -> None:
        self.view = Screen(self.out, color=True)
        frames: list[list[str]] = []
        original = Screen.write_frame

        def record(view: Screen, frame: list[str]) -> None:
            frames.append(frame)
            original(view, frame)

        task = "triage the open issues on bot-bottle-dev and label each one " * 4
        with mock.patch.object(Screen, "write_frame", record):
            self.run_view([*task, "enter", "enter"], name="claude")
        self.assertEqual(task.strip(), self.launched[0][0].prompt)
        art = [screen.strip(row) for row in self.view.art]
        starts = set()
        for frame in frames:
            for line in map(screen.strip, frame):
                self.assertLessEqual(screen.cells(line), 120)  # nothing wraps on the terminal
                if any(row.strip() and line.endswith(row) for row in art):
                    starts.add(screen.cells(line) - screen.ART_WIDTH)
        self.assertEqual(1, len(starts))  # the bottle never slid


class TestSetupAndQuit(_View):
    def test_no_agents_offers_setup_inline(self) -> None:
        for path in (self.root / "agents").iterdir():
            path.unlink()

        def setup(_argv: list[str]) -> int:
            self.agents("claude")
            return 0

        with mock.patch("bot_bottle.cli.commands.setup.cmd_setup", side_effect=setup) as run:
            self.run_view(["enter", "enter"])
        run.assert_called_once_with(["--no-pair"])
        self.assertEqual("claude", self.launched[0][0].agent)
        self.assertIn("set up agents", self.frames())

    def test_ctrl_c_quits_without_launching(self) -> None:
        self.assertEqual(130, self.run_view(["^C"], name="claude"))
        self.assertEqual([], self.launched)
        self.assertFalse(self.view.on)


class TestStartPicksTheView(unittest.TestCase):
    def setUp(self) -> None:
        # pylint: disable-next=consider-using-with
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        agents = Path(self.tmp) / "r" / "agents"
        agents.mkdir(parents=True)
        (agents / "claude.md").write_text("---\nagent_provider:\n  harness: claude\n---\n\nHi.\n")

    def test_a_terminal_start_runs_the_view(self) -> None:
        tmp = self.tmp
        with mock.patch.dict(os.environ, {"HOME": tmp, "BOT_BOTTLE_ROOT": f"{tmp}/r"}), \
             mock.patch.object(start, "wants_guided", return_value=True), \
             mock.patch.object(screen, "usable", return_value=True), \
             mock.patch.object(start.os, "getcwd", return_value=tmp), \
             mock.patch("sys.stdin", _Tty()), \
             mock.patch.object(guided_view, "run_start", return_value=0) as run:
            self.assertEqual(0, start.cmd_start([]))
        run.assert_called_once()

    def test_auto_keeps_the_plain_path(self) -> None:
        tmp = self.tmp
        with mock.patch.dict(os.environ, {"HOME": tmp, "BOT_BOTTLE_ROOT": f"{tmp}/r"}), \
             mock.patch.object(start, "wants_guided", return_value=True), \
             mock.patch.object(screen, "usable", return_value=True), \
             mock.patch.object(start.os, "getcwd", return_value=tmp), \
             mock.patch.object(start, "guided_start", return_value=0) as plain, \
             mock.patch.object(guided_view, "run_start") as view:
            start.cmd_start(["--auto", "t"])
        plain.assert_called_once()
        view.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TestOriginLabel(unittest.TestCase):
    def test_an_ssh_remote_with_a_port_reads_as_host_owner_repo(self) -> None:
        """Not `gitea.dideric.is/30009/didericis/bot-bottle`: the port isn't
        part of the repo's name."""
        import subprocess  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            subprocess.run(["git", "-C", tmp, "remote", "add", "origin",
                            "ssh://git@gitea.dideric.is:30009/didericis/bot-bottle.git"], check=True)
            self.assertEqual("gitea.dideric.is/didericis/bot-bottle",
                             guided_view.origin_of(Path(tmp)))
            self.assertEqual("", guided_view.origin_of(Path(tmp) / "nope"))
