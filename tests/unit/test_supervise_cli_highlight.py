"""Unit: supervise's new-proposal highlight window.

The curses rendering itself is exercised manually; this isolates
the pure decision `is the proposal still in its post-arrival
highlight window?`"""

import unittest

from bot_bottle.cli import supervise as supervise_cli


class TestIsRecent(unittest.TestCase):
    def test_just_seen_is_recent(self):
        self.assertTrue(supervise_cli._is_recent("p1", {"p1": 100.0}, now=100.5))

    def test_seen_within_window(self):
        # Default window is 5s.
        self.assertTrue(
            supervise_cli._is_recent("p1", {"p1": 100.0}, now=104.9),
        )

    def test_seen_past_window_is_not_recent(self):
        self.assertFalse(
            supervise_cli._is_recent("p1", {"p1": 100.0}, now=106.0),
        )

    def test_unknown_proposal_is_not_recent(self):
        self.assertFalse(
            supervise_cli._is_recent("p2", {"p1": 100.0}, now=100.5),
        )

    def test_none_args_safe_default(self):
        self.assertFalse(supervise_cli._is_recent("p1", None, None))
        self.assertFalse(supervise_cli._is_recent("p1", {"p1": 100.0}, None))
        self.assertFalse(supervise_cli._is_recent("p1", None, 100.5))


if __name__ == "__main__":
    unittest.main()
