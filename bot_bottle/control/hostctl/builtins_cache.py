"""Older releases' built-ins (PRD 0131), for merging a release's changes into
a copy of a built-in: the copy's release is the merge's base.

The host controller keeps each release it has run: on start it writes the
installed release's built-ins to `~/.bot-bottle/builtins/<X.Y.Z>/agents/`
and `bottles/` (outside the library's tracked files), so after an upgrade the
release before it is here. A release this host never ran is fetched from the
release's tag on GitHub, where releases are published (the installer and
`upgrade_link` use the same repo), and kept beside them.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from ... import resources
from ...engine import install_version
from ...lib import log
from ...manifest.account_version import is_release, packaged, packaged_files
from ...paths import bot_bottle_root
from .upgrade_link import REPO

CACHE_DIR = "builtins"
# Where each kind is packaged, in the bot-bottle repo.
SOURCE_DIRS = {"agents": "bot_bottle/manifest/builtin_agents",
               "bottles": "bot_bottle/manifest/builtin"}
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", re.ASCII)
_MAX_BYTES = 256 * 1024
# After GitHub couldn't be reached, how long before it's tried again (syncs
# come often, and each would wait out the timeout).
RETRY_SECONDS = 600.0


class BuiltinUnavailable(Exception):
    """A release's built-in couldn't be had: not cached, and not fetched."""


def installed_release() -> str:
    """The installed bot-bottle's version when it's a release; "" for a
    source checkout (its built-ins may be anything) or an unknown one."""
    if resources.is_source_checkout():
        return ""
    version = install_version()[0]
    return version if is_release(version) else ""


def _write(path: Path, text: str) -> None:
    """Write `text` to `path` whole, or not at all."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".new-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise


def fill(root: Path | None = None) -> str:
    """Keep the installed release's built-ins (those not kept already); the
    release, or "" when it isn't one. Best effort."""
    version = installed_release()
    if not version:
        return ""
    folder = (root or bot_bottle_root()) / CACHE_DIR / version
    try:
        for rel, text in packaged_files().items():
            if not (folder / rel).is_file():
                _write(folder / rel, text)
    except OSError as e:
        log.warn(f"couldn't keep bot-bottle {version}'s built-ins: {e}")
    return version


def fetch(kind: str, name: str, version: str, timeout: float = 10.0) -> str | None:
    """Built-in `name` of `kind` at release `version`, from its tag on
    GitHub; None if the release has none. BuiltinUnavailable if it can't be
    reached."""
    if kind not in SOURCE_DIRS or not _NAME.fullmatch(name) or not is_release(version):
        raise ValueError(f"not a built-in: {kind}/{name}@{version}")
    url = (f"https://raw.githubusercontent.com/{REPO}/v{version}/"
           f"{SOURCE_DIRS[kind]}/{name}.md")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # nosec B310: fixed https URL
            body = resp.read(_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise BuiltinUnavailable(f"GitHub answered {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise BuiltinUnavailable(f"couldn't reach GitHub: {getattr(e, 'reason', e)}") from e
    if len(body) > _MAX_BYTES:
        raise BuiltinUnavailable("the file is too large to be a built-in")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise BuiltinUnavailable("the file isn't UTF-8") from e


class Builtins:
    """Built-ins at any release: the installed one's packaged, others from
    the cache, else fetched (and kept)."""

    def __init__(self, root: Path | None = None,
                 fetcher: Callable[[str, str, str], str | None] = fetch,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._root = root
        self._fetch = fetcher
        self._clock = clock
        self._missing: set[tuple[str, str, str]] = set()  # fetched: the release has none
        self._down: tuple[float, str] = (0.0, "")  # GitHub unreachable until, and why

    def at(self, kind: str, name: str, version: str) -> str | None:
        """Built-in `name` of `kind` at release `version`; None if it has none.
        BuiltinUnavailable if that can't be known now."""
        if kind not in SOURCE_DIRS or not _NAME.fullmatch(name) or not is_release(version):
            return None
        if version == installed_release():
            return packaged(kind, name)
        path = (self._root or bot_bottle_root()) / CACHE_DIR / version / kind / f"{name}.md"
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            pass
        except (OSError, UnicodeDecodeError) as e:
            raise BuiltinUnavailable(f"couldn't read {path}: {e}") from e
        if (kind, name, version) in self._missing:
            return None
        if self._clock() < self._down[0]:
            raise BuiltinUnavailable(self._down[1])
        try:
            text = self._fetch(kind, name, version)
        except BuiltinUnavailable as e:
            self._down = (self._clock() + RETRY_SECONDS, str(e))
            raise
        if text is None:
            self._missing.add((kind, name, version))
            return None
        try:
            _write(path, text)
        except OSError as e:
            log.warn(f"couldn't keep bot-bottle {version}'s {kind}/{name}.md: {e}")
        return text


__all__ = ["BuiltinUnavailable", "Builtins", "CACHE_DIR", "RETRY_SECONDS", "SOURCE_DIRS",
           "fetch", "fill", "installed_release"]
