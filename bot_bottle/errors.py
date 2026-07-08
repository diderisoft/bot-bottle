"""bot-bottle application-level exception hierarchy.

Exceptions here are caught by the CLI dispatcher (``cli/__init__.py``)
and rendered as ``bot-bottle: error: …`` lines — same UX as ``die()``,
but without coupling library code to stderr output."""

from __future__ import annotations


class MissingEnvVarError(Exception):
    """Raised when a required host environment variable is unset or empty.

    ``var_name`` is the exact name of the missing variable so callers
    can display it without re-parsing the message string."""

    def __init__(self, var_name: str, message: str) -> None:
        self.var_name = var_name
        super().__init__(message)
