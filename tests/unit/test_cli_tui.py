"""Unit tests for bot_bottle.cli.tui — filter_select and filter_multiselect.

We test the pure-Python logic (_filter_items, cursor movement, confirm,
cancel) by exercising the internal helpers directly, without spinning up
a real curses session (which requires a TTY).
"""

from __future__ import annotations

import unittest
from typing import Any, Optional
from unittest.mock import patch

import curses

from bot_bottle.cli.tui import (
    _filter_items, _multiselect_loop, _picker_loop, filter_multiselect, filter_select,
)

_KEY_SPACE = 32
_KEY_ENTER = 10

_KEY_ESC = 27
_KEY_CTRL_D = 4


class TestFilterItems(unittest.TestCase):
    def setUp(self):
        self.items = ["researcher", "implementer", "codex-researcher", "reviewer"]

    def test_empty_query_returns_all(self):
        self.assertEqual(self.items, _filter_items(self.items, ""))

    def test_query_filters_case_insensitively(self):
        result = _filter_items(self.items, "RESEARCH")
        self.assertEqual(["researcher", "codex-researcher"], result)

    def test_no_match_returns_empty(self):
        self.assertEqual([], _filter_items(self.items, "zzz"))

    def test_partial_match(self):
        result = _filter_items(self.items, "impl")
        self.assertEqual(["implementer"], result)

    def test_empty_items_returns_empty(self):
        self.assertEqual([], _filter_items([], "foo"))


class TestFilterSelectEmptyItems(unittest.TestCase):
    def test_returns_none_for_empty_list(self):
        # No TTY needed — the short-circuit fires before opening tty.
        result = filter_select([], title="Pick one", tty_path="/dev/null")
        self.assertIsNone(result)

    def test_returns_none_when_tty_unavailable(self):
        # /nonexistent is guaranteed to not open.
        with patch("bot_bottle.cli.tui.debug") as debug:
            result = filter_select(["a", "b"], tty_path="/nonexistent/tty")
        self.assertIsNone(result)
        self.assertEqual("FileNotFoundError", debug.call_args.kwargs["context"]["error_type"])


class TestFilterMultiselectEmptyItems(unittest.TestCase):
    def test_returns_empty_list_for_empty_items(self):
        # No TTY needed — short-circuits before opening tty.
        result = filter_multiselect([], title="Select", tty_path="/dev/null")
        self.assertEqual([], result)

    def test_returns_none_when_tty_unavailable(self):
        with patch("bot_bottle.cli.tui.debug") as debug:
            result = filter_multiselect(["a", "b"], tty_path="/nonexistent/tty")
        self.assertIsNone(result)
        self.assertEqual("FileNotFoundError", debug.call_args.kwargs["context"]["error_type"])


class TestMultiselectLoopReordering(unittest.TestCase):
    """Exercise _multiselect_loop key handling without a real curses terminal.

    We drive the loop via a fake screen that feeds a pre-recorded key sequence
    and records what was drawn — we only need the return value, so the fake
    screen's getch() raises StopIteration after the key list is exhausted, and
    the loop is expected to return before that via Ctrl-D.
    """

    def _run(self, keys: list[int], items: list[str], initial: list[str]) -> Optional[list[str]]:
        """Run _multiselect_loop with a synthetic screen feeding `keys`."""
        key_iter = iter(keys)

        class FakeScreen:
            def erase(self) -> None: pass
            def getmaxyx(self) -> tuple[int, int]: return (40, 80)
            def refresh(self) -> None: pass
            def getch(self) -> int: return next(key_iter)
            def addstr(self, *a: Any) -> None: pass
            def keypad(self, *a: Any) -> None: pass

        return _multiselect_loop(FakeScreen(), items, title="", initial=initial)  # type: ignore[arg-type]

    def test_ctrl_d_confirms_initial_selection(self):
        result = self._run([_KEY_CTRL_D], ["a", "b", "c"], ["a", "b"])
        self.assertEqual(["a", "b"], result)

    def test_down_then_space_selects_the_next_item(self):
        result = self._run([curses.KEY_DOWN, _KEY_SPACE, _KEY_CTRL_D], ["a", "b", "c"], [])
        self.assertEqual(["b"], result)

    def test_esc_cancels(self):
        result = self._run([_KEY_ESC], ["a", "b"], ["a"])
        self.assertIsNone(result)

    def test_tab_then_K_moves_item_up(self):
        # Start: selected = ["a", "b", "c"]
        # Tab → order mode (order_cursor=0 on "a")
        # ↓ → order_cursor=1 (on "b")
        # K → swap b and a → ["b", "a", "c"], order_cursor=0
        # Ctrl-D → confirm
        DOWN = ord("j")
        result = self._run(
            [ord("\t"), DOWN, ord("K"), _KEY_CTRL_D],
            ["a", "b", "c"],
            ["a", "b", "c"],
        )
        self.assertEqual(["b", "a", "c"], result)

    def test_tab_then_J_moves_item_down(self):
        # selected = ["a", "b", "c"], focus order, cursor=0
        # J → swap a and b → ["b", "a", "c"], cursor=1
        # Ctrl-D → confirm
        result = self._run(
            [ord("\t"), ord("J"), _KEY_CTRL_D],
            ["a", "b", "c"],
            ["a", "b", "c"],
        )
        self.assertEqual(["b", "a", "c"], result)

    def test_K_at_top_is_no_op(self):
        # cursor already at 0, K should not change order
        result = self._run(
            [ord("\t"), ord("K"), _KEY_CTRL_D],
            ["a", "b"],
            ["a", "b"],
        )
        self.assertEqual(["a", "b"], result)

    def test_J_at_bottom_is_no_op(self):
        DOWN = ord("j")
        result = self._run(
            [ord("\t"), DOWN, ord("J"), _KEY_CTRL_D],
            ["a", "b"],
            ["a", "b"],
        )
        self.assertEqual(["a", "b"], result)

    def test_tab_back_to_filter_then_confirm(self):
        # Tab → order, Tab → filter, Ctrl-D confirms unchanged
        result = self._run(
            [ord("\t"), ord("\t"), _KEY_CTRL_D],
            ["a", "b"],
            ["a", "b"],
        )
        self.assertEqual(["a", "b"], result)

    def test_space_toggles_item_on(self):
        # Space on an unselected item selects it; Ctrl-D confirms.
        result = self._run([_KEY_SPACE, _KEY_CTRL_D], ["a", "b"], [])
        self.assertEqual(["a"], result)

    def test_space_toggles_item_off(self):
        # Space on a selected item deselects it; Ctrl-D confirms empty.
        result = self._run([_KEY_SPACE, _KEY_CTRL_D], ["a", "b"], ["a"])
        self.assertEqual([], result)

    def test_enter_confirms_without_toggle(self):
        # Enter immediately confirms the current selection without toggling.
        result = self._run([_KEY_ENTER], ["a", "b"], ["a"])
        self.assertEqual(["a"], result)

    def test_enter_confirms_empty_selection(self):
        result = self._run([_KEY_ENTER], ["a", "b"], [])
        self.assertEqual([], result)

    def test_space_then_enter_confirms(self):
        # Space selects "a", Enter confirms.
        result = self._run([_KEY_SPACE, _KEY_ENTER], ["a", "b"], [])
        self.assertEqual(["a"], result)



class TestPickerTypingFilters(unittest.TestCase):
    """`typing_filters`: every printable key filters (j/k/q too), arrows move,
    Esc cancels; `header` is drawn but never picked."""

    def _run(self, keys: list[int], items: list[str], **kw: Any) -> tuple[Optional[str], list[str]]:
        key_iter = iter(keys)
        drawn: list[str] = []

        class FakeScreen:
            def erase(self) -> None: pass
            def getmaxyx(self) -> tuple[int, int]: return (40, 120)
            def refresh(self) -> None: pass
            def getch(self) -> int: return next(key_iter)
            def addstr(self, _row: int, _col: int, text: str, *a: Any) -> None:
                drawn.append(text)
            def keypad(self, *a: Any) -> None: pass

        return _picker_loop(FakeScreen(), items, title="t", **kw), drawn  # type: ignore[arg-type]

    def test_j_k_and_q_filter_instead_of_moving_or_cancelling(self) -> None:
        items = ["alpha", "jake", "quinn"]
        picked, _ = self._run([ord("q"), _KEY_ENTER], items, typing_filters=True)
        self.assertEqual("quinn", picked)
        picked, _ = self._run([ord("j"), _KEY_ENTER], items, typing_filters=True)
        self.assertEqual("jake", picked)

    def test_arrows_move_and_esc_cancels(self) -> None:
        items = ["a", "b", "c"]
        picked, _ = self._run([curses.KEY_DOWN, curses.KEY_DOWN, curses.KEY_UP, _KEY_ENTER],
                              items, typing_filters=True)
        self.assertEqual("b", picked)
        self.assertIsNone(self._run([_KEY_ESC], items, typing_filters=True)[0])

    def test_the_header_is_drawn_above_the_items(self) -> None:
        _, drawn = self._run([_KEY_ENTER], ["x"], header="NAME  AGE", typing_filters=True)
        self.assertIn("  NAME  AGE", drawn)
        self.assertLess(drawn.index("  NAME  AGE"), drawn.index("> x"))

    def test_without_it_j_and_q_still_move_and_cancel(self) -> None:
        self.assertEqual("b", self._run([ord("j"), _KEY_ENTER], ["a", "b"])[0])
        self.assertIsNone(self._run([ord("q")], ["a", "b"])[0])


if __name__ == "__main__":
    unittest.main()
