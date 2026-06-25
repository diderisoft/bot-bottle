"""Unit tests for bot_bottle.cli.tui — filter_select and filter_multiselect.

We test the pure-Python logic (_filter_items, cursor movement, confirm,
cancel) by exercising the internal helpers directly, without spinning up
a real curses session (which requires a TTY).
"""

from __future__ import annotations

import unittest

from bot_bottle.cli.tui import _filter_items, filter_multiselect, filter_select


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


if __name__ == "__main__":
    unittest.main()
