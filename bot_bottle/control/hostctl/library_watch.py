"""Hand edits to the library (PRD 0131): while it syncs, the host controller
commits a change to `agents/` or `bottles/` as "edited by hand" once the files
have been quiet for `QUIET_SECONDS`, since editors save in bursts. It looks
on the link's watch loop; a commit then syncs like any other.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

from ...manifest.versions import VersionError, pending_edits, record_hand_edits

QUIET_SECONDS = 2.0


class HandEditWatch:
    """What changed by hand, and since when it's been as it is."""

    def __init__(self, root: Path, clock: Callable[[], float] = time.monotonic) -> None:
        self._root = root
        self._clock = clock
        self._seen: tuple[tuple[str, int], ...] = ()
        self._since = 0.0

    def _changes(self) -> tuple[tuple[str, int], ...]:
        """Each changed path's status and when it was last written (0 when
        it's gone): any save, even of the same text, starts the wait anew."""
        out = []
        for entry in pending_edits(root=self._root):
            try:
                written = os.stat(self._root / entry[3:]).st_mtime_ns
            except OSError:
                written = 0
            out.append((entry, written))
        return tuple(out)

    def poll(self) -> bool:
        """Commit the hand edits once they've been quiet long enough; whether
        it did. VersionError if the commit is refused."""
        changes = self._changes()
        now = self._clock()
        if changes != self._seen:
            self._seen, self._since = changes, now
            return False
        if not changes or now - self._since < QUIET_SECONDS:
            return False
        try:
            record_hand_edits(root=self._root)
        except VersionError:
            self._since = float("inf")  # refused: not again until they change
            raise
        self._seen = ()
        return True


__all__ = ["HandEditWatch", "QUIET_SECONDS"]
