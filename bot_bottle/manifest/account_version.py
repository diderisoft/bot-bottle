"""The account's bot-bottle version (PRD 0131): which release's built-ins a
library means, and the copies of built-ins in it.

- `bot-bottle.lock`, at the library's root, names the version: one line,
  `version = "X.Y.Z"`. Only the console writes it (after an account upgrade);
  a host tracks it and never writes it, and launches on its own installed
  built-ins whatever it says.
- A copy of a built-in is a library file whose frontmatter's first line is
  `copied_from: <name>@<X.Y.Z>`, the release it was copied from. When the
  account's version moves, the release's changes merge into the copy
  (`hostctl/library_upstream.py`). The key means nothing at launch.

The console writes and reads the same forms (`bb_console/library_version.py`),
byte for byte.
"""

from __future__ import annotations

import re
from pathlib import Path

from .loader import BUILTIN_AGENTS_DIR, BUILTIN_BOTTLES_DIR
from .schema import COPIED_FROM_KEY
from .versions import LOCK as LOCK_FILE
# Where each kind of packaged built-in lives, by the library directory a copy
# of it goes in.
BUILTIN_DIRS = {"agents": BUILTIN_AGENTS_DIR, "bottles": BUILTIN_BOTTLES_DIR}

_RELEASE = re.compile(r"\d+\.\d+\.\d+", re.ASCII)
_LOCK_LINE = re.compile(r'^\s*version\s*=\s*"(\d+\.\d+\.\d+)"\s*$', re.ASCII)
_MARKER = re.compile(r"^copied_from:\s*([A-Za-z0-9][A-Za-z0-9._-]{0,127})@(\d+\.\d+\.\d+)\s*$",
                     re.ASCII)


def is_release(version: str) -> bool:
    """Whether `version` is a release, exactly `X.Y.Z`."""
    return bool(_RELEASE.fullmatch(version))


def release_key(version: str) -> tuple[int, int, int]:
    """A release's `(X, Y, Z)`, to compare versions numerically."""
    x, y, z = (int(p) for p in version.split("."))
    return x, y, z


def lock_version(text: str) -> str:
    """The version a lock file's text names; "" for none (the account has no
    version yet)."""
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LOCK_LINE.match(line)
        if m:
            return m.group(1)
    return ""


def mark(content: str, name: str, version: str) -> str:
    """Built-in `name`'s `content` at release `version`, as a copy of it."""
    line = f"{COPIED_FROM_KEY}: {name}@{version}\n"
    if content.startswith("---\n"):
        return "---\n" + line + content[4:]
    return "---\n" + line + "---\n" + content


def copied_from(content: str) -> tuple[str, str] | None:
    """The built-in name and release a copy says it came from, from its
    frontmatter's first line; None for a file that isn't a copy."""
    if not content.startswith("---\n"):
        return None
    first = content[4:].split("\n", 1)[0]
    m = _MARKER.match(first)
    return (m.group(1), m.group(2)) if m else None


def packaged(kind: str, name: str) -> str | None:
    """The installed bot-bottle's built-in `name` of `kind` ("agents" or
    "bottles"), as packaged; None if it has none."""
    folder = BUILTIN_DIRS.get(kind)
    if folder is None or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
        return None
    try:
        return (folder / f"{name}.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def packaged_files() -> dict[str, str]:
    """Every packaged built-in, as `agents/<name>.md` and `bottles/<name>.md`
    to its content, whether or not its harness is ready on this host."""
    out: dict[str, str] = {}
    for kind, folder in BUILTIN_DIRS.items():
        for path in sorted(Path(folder).glob("*.md")) if Path(folder).is_dir() else ():
            try:
                out[f"{kind}/{path.name}"] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
    return out


__all__ = ["BUILTIN_DIRS", "COPIED_FROM_KEY", "LOCK_FILE", "copied_from", "is_release",
           "lock_version", "mark", "packaged", "packaged_files", "release_key"]
