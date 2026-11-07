"""Unit: questions ask through this context's prompter (`lib.prompt`), drawn
inline on a full-screen view (`cli.view_prompt`); without one, the terminal
as before."""

from __future__ import annotations

import contextlib
import io
import unittest
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle import env
from bot_bottle.cli import launch_client, screen
from bot_bottle.cli.commands import setup
from bot_bottle.cli.view_prompt import ViewPrompter
from bot_bottle.git_gate import host_key
from bot_bottle.lib import log, prompt
from bot_bottle.manifest import trust


class _Asker:
    """A prompter that answers from a list, recording what it was asked."""

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.asked: list[tuple[str, str, tuple[str, ...], dict[str, Any]]] = []
        self.went_outside = 0

    def confirm(self, question: str, *, context: Sequence[str] = (),
                default: bool = False) -> bool:
        self.asked.append(("confirm", question, tuple(context), {"default": default}))
        return bool(self.answers.pop(0))

    def ask(self, question: str, *, context: Sequence[str] = (), secret: bool = False,
            default: str = "") -> str:
        del default  # what's answered is the test's
        self.asked.append(("ask", question, tuple(context), {"secret": secret}))
        return str(self.answers.pop(0))

    @contextlib.contextmanager
    def outside(self) -> Generator[None]:
        self.went_outside += 1
        yield


class TestTheSink(unittest.TestCase):
    def test_none_by_default_and_restored_after(self) -> None:
        asker = _Asker()
        self.assertIsNone(prompt.current())
        with prompt.redirect(asker):
            self.assertIs(asker, prompt.current())
            with prompt.redirect(None):
                self.assertIsNone(prompt.current())
            self.assertIs(asker, prompt.current())
        self.assertIsNone(prompt.current())

    def test_outside_steps_aside_and_asks_the_terminal(self) -> None:
        asker = _Asker()
        with prompt.redirect(asker):
            with prompt.outside():
                self.assertIsNone(prompt.current())
            self.assertIs(asker, prompt.current())
        self.assertEqual(1, asker.went_outside)

    def test_outside_without_a_view_does_nothing(self) -> None:
        with prompt.outside():
            self.assertIsNone(prompt.current())


class TestViewPrompter(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch.object(screen, "size", return_value=(80, 24)))
        self.shown: list[tuple[list[str] | None, str]] = []

    def prompter(self, keys: list[str], width: int = 40) -> ViewPrompter:
        feed = iter(keys)
        return ViewPrompter(lambda lines, footer: self.shown.append((lines, footer)),
                            lambda: next(feed), width=lambda: width,
                            style=screen.Style(False), suspend=contextlib.nullcontext)

    def test_context_wraps_under_its_indent(self) -> None:
        self.prompter(["y"], width=24).confirm("ok?", context=[
            "    reach api.example.com and also files.example.com"])
        lines = self.shown[0][0] or []
        self.assertEqual(["    reach", "    api.example.com and", "    also",
                          "    files.example.com", ""], lines[:5])

    def test_a_long_context_is_cut_to_the_rows_there_are(self) -> None:
        self.prompter(["n"]).confirm("ok?", context=[f"grant {n}" for n in range(40)])
        lines = self.shown[0][0] or []
        self.assertLess(len(lines), 24)
        self.assertIn("more lines", "".join(lines))

    def test_answered_drops_the_prompt(self) -> None:
        self.prompter(["y"]).confirm("ok?")
        self.assertEqual((None, ""), self.shown[-1])

    def test_ctrl_c_interrupts(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self.prompter(["ctrl-c"]).ask("name")
        self.assertEqual((None, ""), self.shown[-1])

    def test_outside_is_the_views_suspend(self) -> None:
        suspend = mock.MagicMock()
        def show(_lines: list[str] | None, _footer: str) -> None:
            pass

        asker = ViewPrompter(show, lambda: None, width=lambda: 40,
                             style=screen.Style(False), suspend=suspend)
        with asker.outside():
            pass
        suspend.assert_called_once()


def _untrusted(previous: trust.Trusted | None = None) -> trust.UntrustedBottleError:
    return trust.UntrustedBottleError("./main", Path("/p/.bot-bottle/bottles/main.md"),
                                      digest="d", granted=["reach api.example.com (GET)"],
                                      previous=previous)


class TestTrust(unittest.TestCase):
    def test_parts_are_the_terminals_question(self) -> None:
        lines, question = trust.prompt_parts(_untrusted())
        self.assertEqual("untrusted bottle: ./main  (/p/.bot-bottle/bottles/main.md)", lines[0])
        self.assertEqual("Trust this bottle on this host?", question)
        self.assertEqual("\n".join([*lines, "", f"{question} [y/N] "]),
                         trust.prompt_text(_untrusted()))

    def test_asks_the_prompter_with_what_it_grants(self) -> None:
        asker = _Asker(True)
        with prompt.redirect(asker):
            self.assertTrue(launch_client._trusts(_untrusted()))  # pylint: disable=protected-access
        kind, question, context, _ = asker.asked[0]
        self.assertEqual(("confirm", "Trust this bottle on this host?"), (kind, question))
        self.assertIn("    reach api.example.com (GET)", context)

    def test_without_one_reads_the_terminal(self) -> None:
        err = io.StringIO()
        with mock.patch.object(launch_client, "read_tty_line", return_value="y"), \
             mock.patch("sys.stderr", err):
            self.assertTrue(launch_client._trusts(_untrusted()))  # pylint: disable=protected-access
        self.assertIn("Trust this bottle on this host? [y/N] ", err.getvalue())


class TestControllerRestart(unittest.TestCase):
    def test_asks_the_prompter_with_what_moves(self) -> None:
        asker = _Asker(False)
        with prompt.redirect(asker), mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = True
            self.assertFalse(launch_client.confirm_controller_restart(["a", "b"]))
        _, question, context, _ = asker.asked[0]
        self.assertEqual("Continue?", question)
        self.assertIn("2 running bottle(s) (a, b)", context[0])

    def test_without_one_reads_the_terminal(self) -> None:
        err = io.StringIO()
        with mock.patch.object(launch_client, "read_tty_line", return_value="n"), \
             mock.patch("sys.stderr", err), mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = True
            self.assertFalse(launch_client.confirm_controller_restart(["a"]))
        self.assertIn("bot-bottle: bot-bottle was updated.", err.getvalue())
        self.assertIn("bot-bottle: Continue? [y/N] ", err.getvalue())


class TestHostKey(unittest.TestCase):
    def test_asks_the_prompter_with_the_key(self) -> None:
        asker = _Asker(True)
        with prompt.redirect(asker):
            self.assertTrue(host_key._yes("Is this host key correct?",  # pylint: disable=protected-access
                                          ["git-gate: host key for r:", "  ssh-ed25519 AAAA"]))
        self.assertEqual(("ssh-ed25519 AAAA",), tuple(c.strip() for c in asker.asked[0][2][1:]))

    def test_without_one_reads_the_terminal(self) -> None:
        err = io.StringIO()
        with mock.patch.object(host_key, "prompt_tty", return_value="yes") as tty, \
             mock.patch("sys.stderr", err):
            self.assertTrue(host_key._yes("ok?", ["key"]))  # pylint: disable=protected-access
        tty.assert_called_once_with("ok? [y/N] ")
        self.assertIn("key", err.getvalue())


class TestSecret(unittest.TestCase):
    def test_asks_the_prompter_hidden(self) -> None:
        asker = _Asker("v4lue")
        with prompt.redirect(asker):
            self.assertEqual("v4lue", env.prompt_secret("API_KEY", "?"))
        kind, question, _, opts = asker.asked[0]
        self.assertEqual(("ask", {"secret": True}), (kind, opts))
        self.assertIn("API_KEY", question)

    def test_an_empty_answer_stops_the_launch(self) -> None:
        with prompt.redirect(_Asker("")), self.assertRaises(log.Die):
            env.prompt_secret("API_KEY", "?")


class TestSetupOnAView(unittest.TestCase):
    def test_nothing_changes_without_one(self) -> None:
        self.assertEqual({}, setup._on_a_view())  # pylint: disable=protected-access

    def test_its_questions_and_lines_go_to_the_view(self) -> None:
        asker = _Asker(True, "the-code")
        lines: list[str] = []
        with prompt.redirect(asker), log.redirect(lines.append):
            io_ = setup._on_a_view()  # pylint: disable=protected-access
            run = setup.Setup(home=Path("/nowhere"), host_env={}, **io_)
            self.assertTrue(run.interactive)
            self.assertTrue(run.confirm("pi: sign in?"))
            self.assertEqual("the-code", run.ask("Code: "))
            run.say("wrote it")
        self.assertEqual(["wrote it"], lines)
        self.assertEqual(("confirm", "pi: sign in?", (), {"default": True}), asker.asked[0])
        self.assertEqual("Code", asker.asked[1][1])

    def test_pairing_steps_outside_the_view(self) -> None:
        asker = _Asker(True)
        run = setup.Setup(home=Path("/nowhere"), host_env={}, interactive=True,
                          yes_no=lambda _q: True)
        seen: list[object] = []
        with prompt.redirect(asker), \
             mock.patch.object(setup.pairing, "load_console",
                               side_effect=setup.pairing.RemoteError("no")), \
             mock.patch("bot_bottle.cli.commands.pair.cmd_pair",
                        side_effect=lambda _argv: seen.append(  # pyright: ignore
                            prompt.current())):
            setup._offer_pair(run, "https://console")  # pylint: disable=protected-access
        self.assertEqual([None], seen)
        self.assertEqual(1, asker.went_outside)


if __name__ == "__main__":
    unittest.main()
