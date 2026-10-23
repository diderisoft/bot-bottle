"""Where bottle secrets come from.

A `SecretSource` maps a secret's name (an egress `token_ref`, an
interpolated `${HOST_VAR}`, a `?` env entry, a forge token env name) to its
value. Prepare and launch take one instead of reading the environment:

- `MappingSecrets` — a fixed name→value mapping (values a CLI forwards).
- `FileSecrets` — the host secrets file (`NAME=value` lines, mode 0600).
- `ChainSecrets` — several sources in order; the first hit wins.

No source ever renders a value in its repr or in an error message.
"""

from __future__ import annotations

import os
import re
import stat
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path

from .errors import SecretsFileError

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SecretSource(ABC):
    """Looks up secret values by name."""

    @abstractmethod
    def get(self, name: str) -> str | None:
        """The value of `name`, or None when this source doesn't have it."""


class MappingSecrets(SecretSource):
    """Secrets from a fixed name→value mapping."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def __repr__(self) -> str:
        return f"MappingSecrets(<{len(self._values)} values>)"


class FileSecrets(SecretSource):
    """Secrets from a `NAME=value` file.

    Blank lines and `#` comments are skipped; each value is the text after
    the first `=`, taken literally (no quotes, no expansion). A missing file
    holds no secrets. The file is read on first lookup and must be owned by
    this user with no group/other permissions, or lookups raise
    `SecretsFileError`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._values: dict[str, str] | None = None

    def get(self, name: str) -> str | None:
        if self._values is None:
            self._values = self._load()
        return self._values.get(name)

    def _load(self) -> dict[str, str]:
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return {}
        except OSError as e:
            raise SecretsFileError(f"secrets file {self.path}: cannot open: {e.strerror}") from e
        with os.fdopen(fd, encoding="utf-8") as f:
            st = os.fstat(f.fileno())
            if st.st_uid != os.getuid():
                raise SecretsFileError(
                    f"secrets file {self.path} is not owned by the current user; "
                    "refusing to read it")
            if stat.S_IMODE(st.st_mode) & 0o077:
                raise SecretsFileError(
                    f"secrets file {self.path} is accessible to group or others; "
                    f"run `chmod 600 {self.path}`")
            return _parse(self.path, f.read())

    def __repr__(self) -> str:
        return f"FileSecrets({str(self.path)!r})"


class ChainSecrets(SecretSource):
    """Asks each source in order; the first one that has the name wins."""

    def __init__(self, *sources: SecretSource) -> None:
        self.sources = sources

    def get(self, name: str) -> str | None:
        for source in self.sources:
            value = source.get(name)
            if value is not None:
                return value
        return None

    def __repr__(self) -> str:
        return f"ChainSecrets({', '.join(map(repr, self.sources))})"


def _parse(path: Path, text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if not sep or not _NAME_RE.match(name):
            # Never echo the line: it may hold a value.
            raise SecretsFileError(
                f"secrets file {path}: line {lineno} is not NAME=value")
        values[name] = value
    return values


__all__ = ["ChainSecrets", "FileSecrets", "MappingSecrets", "SecretSource"]
