"""Unit tests for bot_bottle.cli.tui — filter_select and filter_multiselect.

We test the pure-Python logic (_filter_items, cursor movement, confirm,
cancel) by exercising the internal helpers directly, without spinning up
a real curses session (which requires a TTY).
"""

from __future__ import annotations

import unittest

from bot_bottle.cli.tui import _filter_items, _multiselect_loop, filter_multiselect, filter_select


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
        result = filter_select(["a", "b"], tty_path="/nonexistent/tty")
        self.assertIsNone(result)


class TestFilterMultiselectEmptyItems(unittest.TestCase):
    def test_returns_empty_list_for_empty_items(self):
        # No TTY needed — short-circuits before opening tty.
        result = filter_multiselect([], title="Select", tty_path="/dev/null")
        self.assertEqual([], result)

    def test_returns_none_when_tty_unavailable(self):
        result = filter_multiselect(["a", "b"], tty_path="/nonexistent/tty")
        self.assertIsNone(result)


class TestMultiselectLoopReordering(unittest.TestCase):
    """Exercise _multiselect_loop key handling without a real curses terminal.

    We drive the loop via a fake screen that feeds a pre-recorded key sequence
    and records what was drawn — we only need the return value, so the fake
    screen's getch() raises StopIteration after the key list is exhausted, and
    the loop is expected to return before that via Ctrl-D.
    """

    def _run(self, keys: list[int], items: list[str], initial: list[str]) -> Optional[list[str]]:
        """Run _multiselect_loop with a synthetic screen feeding `keys`."""
        import curses as _curses

        key_iter = iter(keys)

        class FakeScreen:
            def erase(self): pass
            def getmaxyx(self): return (40, 80)
            def refresh(self): pass
            def getch(self):
                return next(key_iter)
            def addstr(self, *a, **kw): pass
            def keypad(self, *a): pass

        return _multiselect_loop(FakeScreen(), items, title="", initial=initial)  # type: ignore[arg-type]

    def test_ctrl_d_confirms_initial_selection(self):
        result = self._run([_KEY_CTRL_D], ["a", "b", "c"], ["a", "b"])
        self.assertEqual(["a", "b"], result)

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


from typing import Optional
_KEY_ESC = 27
_KEY_CTRL_D = 4


if __name__ == "__main__":
    unittest.main()
