"""Tabular CLI output: aligned columns under a header in a terminal, one
tab-separated line per row when piped (what scripts read, unchanged).

Tab stops can't line up cells of very different widths, so a terminal gets
cells padded to their column's widest, two spaces apart.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence

# (row index, column index, padded cell) -> the cell as printed, e.g. coloured.
Style = Callable[[int, int, str], str]


def print_table(header: Sequence[str], rows: Sequence[Sequence[str]], *,
                style: Style | None = None, drop_empty: bool = False,
                empty: str = "-") -> None:
    """Print ``rows`` under ``header`` when stdout is a terminal, else as
    tab-separated lines with ``empty`` for a blank cell. ``style`` dresses a
    cell after padding, so its escape codes don't skew the widths.
    ``drop_empty`` leaves out a column that's blank in every row."""
    if not sys.stdout.isatty():
        for row in rows:
            print("\t".join(cell or empty for cell in row))
        return
    keep = list(range(len(header)))
    if drop_empty:
        keep = [i for i in keep if any(row[i] for row in rows)]
    table = [[header[i] for i in keep], *([row[i] for i in keep] for row in rows)]
    widths = [max(len(line[i]) for line in table) for i in range(len(keep))]
    for r, line in enumerate(table):
        cells = []
        for c, cell in enumerate(line):
            padded = cell.ljust(widths[c])
            cells.append(style(r - 1, keep[c], padded) if style and r else padded)
        print("  ".join(cells).rstrip())


__all__ = ["Style", "print_table"]
