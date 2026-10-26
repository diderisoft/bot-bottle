"""Where bottle secrets come from. A `SecretSource` maps a secret's name to its
value; prepare and launch take one instead of reading the environment.

- `MappingSecrets`: a fixed mapping (values a CLI forwards).
- `FileSecrets`: the host secrets file.
- `ChainSecrets`: several sources; the first hit wins.

No source renders a value in its repr or an error.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path

from .errors import SecretsFileError

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SecretSource(ABC):
    """Looks up secret values by name."""

    @abstractmethod
    def get(self, name: str) -> str | None:
        """The value of `name`, or None."""


class MappingSecrets(SecretSource):
    """Secrets from a fixed name→value mapping."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def __repr__(self) -> str:
        return f"MappingSecrets(<{len(self._values)} values>)"


class FileSecrets(SecretSource):
    """Secrets from a `NAME=value` file: blank lines and `#` comments skipped,
    the value taken literally after the first `=`. A missing file holds
    nothing. Read on first lookup; it must be owned by this user with no
    group/other access, or lookups raise `SecretsFileError`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._values: dict[str, str] | None = None

    def get(self, name: str) -> str | None:
        if self._values is None:
            self._values = self._load()
        return self._values.get(name)

    def items(self) -> list[tuple[str, str]]:
        """Every `(name, value)` in the file."""
        if self._values is None:
            self._values = self._load()
        return list(self._values.items())

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
    """Asks each source in order; the first hit wins."""

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
            # Never echo the line.
            raise SecretsFileError(
                f"secrets file {path}: line {lineno} is not NAME=value")
        values[name] = value
    return values


def write_secret(path: Path, name: str, value: str) -> None:
    """Set `name` in the secrets file at `path`, replacing or appending its
    line. Rewritten atomically at 0600; the value is never echoed."""
    if not _NAME_RE.match(name):
        raise SecretsFileError(f"secret name {name!r} is not NAME-shaped")
    if not value or "\n" in value or "\r" in value:
        raise SecretsFileError(f"secret {name}: the value must be one non-empty line")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    kept = [line for line in lines if line.strip().partition("=")[0] != name]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".secrets-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("".join(f"{line}\n" for line in [*kept, f"{name}={value}"]))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


__all__ = ["ChainSecrets", "FileSecrets", "MappingSecrets", "SecretSource", "write_secret"]
