"""Shared CLI constants.

Kept as a leaf module (imports nothing from the `cli` package) so both the
dispatcher (`cli/__init__.py`) and the command modules it imports can share
`PROG` without a circular import.
"""

PROG = "cli.py"
