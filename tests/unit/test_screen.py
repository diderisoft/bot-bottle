"""The shared terminal view: frames (without a real terminal), the step list,
the menu, keys, and the launch drawing under a guided start's steps."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.cli import progress, screen
from bot_bottle.cli.screen import (
    CURRENT, DONE, PENDING, RUNNING, Screen, Step, Style, compose, decode_key, edit_line,
    menu_lines, step_lines, strip,
)

PLAIN = Style(color=False)


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class TestCompose(unittest.TestCase):
    ART = ["#" * screen.ART_WIDTH] * 10

    def test_left_beside_the_bottle_and_the_footer_on_the_last_row(self) -> None:
        frame = compose(["a", "b"], self.ART, columns=120, rows=30, left_width=40,
                        footer="enter run", align="top")
        self.assertEqual(30, len(frame))
        self.assertIn("enter run", frame[-1])
        art_rows = [line for line in frame if "#" in line]
        self.assertEqual(10, len(art_rows))
        self.assertTrue(art_rows[0].lstrip().startswith("a"))  # top-aligned with the bottle

    def test_center_puts_a_short_column_level_with_the_bottle(self) -> None:
        frame = compose(["mid"], self.ART, columns=120, rows=30, left_width=40)
        row = next(i for i, line in enumerate(frame) if "mid" in line)
        art = [i for i, line in enumerate(frame) if "#" in line]
        self.assertEqual(art[0] + 4, row)

    def test_a_narrow_terminal_trades_the_bottle_for_a_header(self) -> None:
        frame = compose(["a"], self.ART, columns=60, rows=30, left_width=40, header="▣ bot-bottle")
        self.assertFalse(any("#" in line for line in frame))
        text = [line.strip() for line in frame if line.strip()]
        self.assertEqual(["▣ bot-bottle", "a"], text)

    def test_without_art_there_is_no_bottle(self) -> None:
        frame = compose(["a"], [], columns=120, rows=20, left_width=40)
        self.assertEqual(["a"], [line.strip() for line in frame if line.strip()])


class TestSteps(unittest.TestCase):
    def test_each_state_has_its_mark(self) -> None:
        steps = [Step("Project", "~/app", DONE), Step("Agent", "claude", DONE),
                 Step("Task", state=CURRENT, body=["› fix it▌"]),
                 Step("Bottle", "asking the architect…", RUNNING, started=100.0),
                 Step("Launch", state=PENDING)]
        lines = step_lines(steps, width=60, style=PLAIN, now=104.0)
        self.assertEqual("✓ Project    ~/app", lines[0])
        self.assertEqual("▸ Task", lines[2])
        self.assertEqual("    › fix it▌", lines[3])
        self.assertTrue(lines[4].endswith("asking the architect…  4s"))
        self.assertEqual("· Launch", lines[5])

    def test_long_values_are_cut_to_the_width(self) -> None:
        line = step_lines([Step("Task", "x" * 200, DONE)], width=40, style=PLAIN)[0]
        self.assertLessEqual(len(line), 40)
        self.assertTrue(line.endswith("…"))

    def test_color_marks_done_green(self) -> None:
        line = step_lines([Step("Agent", "claude", DONE)], width=40, style=Style(True))[0]
        self.assertIn("\x1b[32m✓", line)
        self.assertEqual("✓ Agent      claude", strip(line))

    def test_the_menu_marks_the_cursor(self) -> None:
        lines = menu_lines([("Start", ""), ("Agents", "2 running")], 1, width=60, style=PLAIN)
        self.assertTrue(lines[0].startswith("  Start"))
        self.assertTrue(lines[1].startswith("› Agents"))
        self.assertIn("2 running", lines[1])


class TestKeys(unittest.TestCase):
    def test_decode(self) -> None:
        self.assertEqual("up", decode_key("\x1b[A"))
        self.assertEqual("down", decode_key("\x1bOB"))
        self.assertEqual("esc", decode_key("\x1b"))
        self.assertEqual("enter", decode_key("\r"))
        self.assertEqual("backspace", decode_key("\x7f"))
        self.assertEqual("ctrl-c", decode_key("\x03"))
        self.assertEqual("é", decode_key("é"))

    def test_line_editing(self) -> None:
        self.assertEqual("ab", edit_line("a", "b"))
        self.assertEqual("a", edit_line("ab", "backspace"))
        self.assertEqual("", edit_line("abc", "ctrl-u"))
        self.assertEqual("ab", edit_line("ab", "up"))


class TestScreen(unittest.TestCase):
    def test_no_color_means_no_bottle(self) -> None:
        with mock.patch.dict("os.environ", {"NO_COLOR": "1"}):
            view = Screen(_Tty())
        self.assertFalse(view.color)
        self.assertEqual([], view.art)

    def test_draw_enters_once_and_rewrites_only_changes(self) -> None:
        out = _Tty()
        view = Screen(out, color=False)
        with mock.patch.object(screen, "size", return_value=(100, 20)):
            view.draw(["one", "two"])
            first = out.getvalue()
            view.draw(["one", "three"])
        self.assertEqual(1, out.getvalue().count(screen.ENTER))
        later = out.getvalue()[len(first):]
        self.assertIn("three", later)
        self.assertNotIn("one", later)
        view.close()
        self.assertTrue(out.getvalue().endswith(screen.LEAVE))

    def test_suspend_hands_back_the_normal_screen(self) -> None:
        out = _Tty()
        view = Screen(out, color=False)
        with mock.patch.object(screen, "size", return_value=(100, 20)):
            view.draw(["x"])
            with view.suspend():
                self.assertFalse(view.on)
            self.assertTrue(view.on)
        view.close()

    def test_usable_needs_the_real_terminal(self) -> None:
        with mock.patch("sys.stdin", io.StringIO()):
            self.assertFalse(screen.usable())


class TestLaunchInTheView(unittest.TestCase):
    def test_stages_draw_under_the_steps_without_clearing(self) -> None:
        out = _Tty()
        view = Screen(out, color=False)
        with mock.patch.object(screen, "size", return_value=(110, 30)), \
             mock.patch.object(progress, "_size", return_value=(110, 30)), \
             tempfile.TemporaryDirectory() as tmp:
            view.draw(["▸ Bottle"])
            launch = progress.LaunchProgress(Path(tmp) / "log", view=view,
                                             lead=[Step("Agent", "claude", DONE)])
            launch.stage("bottle", "Starting the bottle")
            frame = launch._layout()  # pylint: disable=protected-access
            launch.close()
        self.assertEqual(1, out.getvalue().count(screen.ENTER))
        text = [line.strip() for line in frame if line.strip()]
        self.assertEqual("✓ Agent      claude", text[1])  # under the view's header
        self.assertIn("Starting the bottle", text[2])


if __name__ == "__main__":
    unittest.main()
