"""What a backend leaves on the host besides bottles: infra containers,
networks, volumes and images, for `bot-bottle uninstall`.

Each backend lists its own by bot-bottle's names, never by guessing; base
images it pulled to build on (python, node, alpine...) aren't bot-bottle's
and stay. Frozen bottles (`bot-bottle-committed-*` images) are the user's
work, so they go only with `--purge`.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

# Every image bot-bottle builds or pulls is named `bot-bottle-<something>`.
IMAGE_PREFIX = "bot-bottle-"
# A frozen bottle's image (`commit`), kept unless purging.
COMMITTED_PREFIX = "bot-bottle-committed-"


@dataclass(frozen=True)
class HostResource:
    """One thing to remove: `what` for the plan, `remove()` to do it (False
    when it couldn't be removed)."""

    what: str
    remove: Callable[[], bool]


def is_bot_bottle_image(ref: str, *, purge: bool) -> bool:
    """Whether `ref` (any registry, tag or digest) names a bot-bottle image,
    frozen bottles only when purging."""
    repository = ref.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0]
    if not repository.startswith(IMAGE_PREFIX):
        return False
    return purge or not repository.startswith(COMMITTED_PREFIX)


def run_ok(argv: Sequence[str]) -> bool:
    """Run `argv` quietly; whether it succeeded."""
    try:
        return subprocess.run(list(argv), capture_output=True, check=False).returncode == 0
    except OSError:
        return False


def run_json(argv: Sequence[str]) -> list[dict[str, object]]:
    """`argv`'s JSON output as a list of objects; empty when it fails."""
    try:
        out = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    except OSError:
        return []
    if out.returncode != 0 or not out.stdout.strip():
        return []
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def run_lines(argv: Sequence[str]) -> list[str]:
    """`argv`'s output lines, stripped; empty when it fails."""
    try:
        out = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    except OSError:
        return []
    return [] if out.returncode != 0 else [ln.strip() for ln in out.stdout.splitlines()
                                           if ln.strip()]


__all__ = ["COMMITTED_PREFIX", "HostResource", "IMAGE_PREFIX", "is_bot_bottle_image",
           "run_json", "run_lines", "run_ok"]
