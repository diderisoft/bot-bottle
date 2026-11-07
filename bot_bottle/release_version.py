"""Picking a release's version number: the first `MAJOR.MINOR.PATCH` from a
start that nothing has taken yet, bumping the patch, and the versions the
repo's `vX.Y.Z` git tags have already taken."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

RELEASE_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def git_release_tags(repo_root: Path) -> frozenset[str]:
    """Versions that already have a `vX.Y.Z` git tag in the repo."""
    try:
        out = subprocess.run(
            ["git", "tag", "--list", "v*"],
            capture_output=True, text=True, check=True,
            cwd=repo_root,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return frozenset()
    tagged = set()
    for line in out.splitlines():
        tag = line.strip()
        if tag.startswith("v") and RELEASE_VERSION.match(tag[1:]):
            tagged.add(tag[1:])
    return frozenset(tagged)


def next_free(start: str, taken: Callable[[str], bool]) -> str:
    """The first version from `start` up that isn't `taken`, bumping the patch.
    Raises ValueError if `start` isn't a release version."""
    match = RELEASE_VERSION.match(start)
    if not match:
        raise ValueError(f"{start!r} is not a MAJOR.MINOR.PATCH release version")
    major, minor, patch = (int(g) for g in match.groups())
    while taken(version := f"{major}.{minor}.{patch}"):
        patch += 1
    return version
