"""The `Bot-Bottle` commit trailer, shared by the host (which provisions the
hook that adds it) and git-gate (which checks pushes carry it).

Every commit made in a bottle carries `Bot-Bottle: <bottle>/<run>`: the
bottle names the launch was built from, and the bottled agent's identity
(its slug), which is minted at start and kept across stop and resume.
"""

from __future__ import annotations

import re

TRAILER_KEY = "Bot-Bottle"

# How git-gate treats a pushed commit whose trailer is missing or names
# another bottle: refuse the push, accept it with a warning, or don't check.
TRAILER_REQUIRE = "require"
TRAILER_WARN = "warn"
TRAILER_OFF = "off"
TRAILER_MODES = (TRAILER_REQUIRE, TRAILER_WARN, TRAILER_OFF)
DEFAULT_TRAILER_MODE = TRAILER_REQUIRE

# A trailer value is embedded in gitconfig, shell and the hook's messages, so
# it is kept to a path- and shell-safe alphabet.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
TRAILER_VALUE_RE = re.compile(r"[A-Za-z0-9._+-]+/[A-Za-z0-9._-]+")


def trailer_value(bottle_names: tuple[str, ...] | list[str], run: str) -> str:
    """`<bottle>/<run>`: the bottle names joined by `+` (each reduced to
    `[A-Za-z0-9._-]`), and the run. Raises ValueError for an empty part."""
    parts = [_UNSAFE.sub("-", n).strip("-.") for n in bottle_names]
    bottle = "+".join(p for p in parts if p)
    run_part = _UNSAFE.sub("-", run).strip("-.")
    if not bottle or not run_part:
        raise ValueError(f"commit trailer needs a bottle and a run "
                         f"(got {list(bottle_names)!r}, {run!r})")
    return f"{bottle}/{run_part}"


def trailer_mode(raw: str) -> str:
    """`raw` as a trailer mode; "" is the default. Raises ValueError."""
    mode = raw.strip() or DEFAULT_TRAILER_MODE
    if mode not in TRAILER_MODES:
        raise ValueError(f"{raw!r} is not one of {', '.join(TRAILER_MODES)}")
    return mode


__all__ = [
    "DEFAULT_TRAILER_MODE", "TRAILER_KEY", "TRAILER_MODES", "TRAILER_OFF",
    "TRAILER_REQUIRE", "TRAILER_VALUE_RE", "TRAILER_WARN", "trailer_mode",
    "trailer_value",
]
