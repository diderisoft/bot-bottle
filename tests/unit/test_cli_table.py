"""Tabular CLI output: aligned in a terminal, tab-separated when piped."""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from typing import Any

from bot_bottle.cli.table import print_table

_HEADER = ("NAME", "AGENT", "LABEL")
_ROWS = [("a-much-longer-name", "claude", ""), ("x", "pi", "")]


def _run(tty: bool, **kw: Any) -> str:
    out = io.StringIO()
    out.isatty = lambda: tty  # type: ignore[method-assign]
    with patch("sys.stdout", out):
        print_table(_HEADER, _ROWS, **kw)
    return out.getvalue()


class TestPrintTable(unittest.TestCase):
    def test_piped_is_one_tab_separated_line_per_row(self) -> None:
        self.assertEqual("a-much-longer-name\tclaude\t-\nx\tpi\t-\n", _run(False))

    def test_a_terminal_gets_aligned_columns_under_a_header(self) -> None:
        lines = _run(True).splitlines()
        self.assertEqual(["NAME", "a-much-longer-name", "x"], [ln.split()[0] for ln in lines])
        self.assertEqual({lines[0].index("AGENT")},
                         {lines[1].index("claude"), lines[2].index("pi")})
        self.assertTrue(all(ln == ln.rstrip() for ln in lines))

    def test_a_blank_column_can_be_left_out(self) -> None:
        self.assertNotIn("LABEL", _run(True, drop_empty=True))
        self.assertIn("LABEL", _run(True))

    def test_style_doesnt_skew_the_widths(self) -> None:
        def red(_row: int, col: int, cell: str) -> str:
            return f"\x1b[91m{cell}\x1b[0m" if col == 0 else cell

        lines = _run(True, style=red).splitlines()
        plain = [ln.replace("\x1b[91m", "").replace("\x1b[0m", "") for ln in lines]
        self.assertEqual({plain[0].index("AGENT")},
                         {plain[1].index("claude"), plain[2].index("pi")})
        self.assertNotIn("\x1b", lines[0])  # the header isn't styled


if __name__ == "__main__":
    unittest.main()
