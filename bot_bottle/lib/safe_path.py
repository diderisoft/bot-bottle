"""Path parts that come from data (a host DB row, an API body, an
orchestrator reply) and the paths built from them.

An id (a bottled agent's identity, a session, an activation) is checked when
it's written (`check_id`). A path built from rows is checked again before it's
read or deleted (`under`), so a row written some other way can't reach outside
its root.
"""

from __future__ import annotations

import re
from pathlib import Path

# A UUID, a hex registry id, or a test's `demo-1`: no `/`, no leading `.`.
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def is_id(value: object) -> bool:
    """Whether `value` is safe as one path part (`_ID_RE`)."""
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def check_id(value: object, what: str) -> str:
    """`value`, or ValueError naming `what` when it isn't `is_id`."""
    if not is_id(value):
        raise ValueError(f"{what} must be letters, digits, - and _ (was {value!r})")
    return str(value)


def under(root: Path, *parts: str) -> Path:
    """`root / parts...`, or ValueError when it resolves outside `root` (or is
    `root` itself), as `..`, an absolute part or a symlink would make it."""
    path = root.joinpath(*parts)
    base = root.resolve()
    resolved = path.resolve()
    if resolved == base or not resolved.is_relative_to(base):
        raise ValueError(f"{path} isn't under {root}")
    return path


__all__ = ["check_id", "is_id", "under"]
