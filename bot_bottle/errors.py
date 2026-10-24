"""Application exceptions, rendered by the CLI as `bot-bottle: error: ...`."""

from __future__ import annotations


class MissingEnvVarError(Exception):
    """A required host env var is unset or empty; `var_name` names it."""

    def __init__(self, var_name: str, message: str) -> None:
        self.var_name = var_name
        super().__init__(message)


class SecretsFileError(Exception):
    """The host secrets file can't be trusted or parsed. Names the file and
    line, never a value."""
