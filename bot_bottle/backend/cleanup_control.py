"""Shared destructive-cleanup execution and failure accounting."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from ..environment import resource_prefix


class CleanupError(RuntimeError):
    """One or more approved cleanup mutations did not complete."""


class CleanupFailures:
    """Attempt every approved mutation, then fail with full diagnostics. Each
    command gets `timeout` seconds (at least one)."""

    def __init__(self, *, timeout: float) -> None:
        self._messages: list[str] = []
        self._timeout = max(timeout, 1.0)

    def run(self, argv: Sequence[str], description: str) -> None:
        try:
            result = subprocess.run(
                list(argv), capture_output=True, text=True, check=False,
                timeout=self._timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._messages.append(f"{description}: {exc}")
            return
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            self._messages.append(
                f"{description}: {detail or f'exit {result.returncode}'}"
            )

    def remove_tree(self, path: Path, description: str) -> None:
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            self._messages.append(f"{description}: {exc}")

    def record(self, message: str) -> None:
        self._messages.append(message)

    def raise_if_any(self) -> None:
        if self._messages:
            raise CleanupError("; ".join(self._messages))


def orphan_homes(volumes: "list[str] | tuple[str, ...]") -> list[str]:
    """The bottled agents' home volumes among `volumes` (`home_volume_name`)
    whose state isn't kept for resume: the ones cleanup removes."""
    # pylint: disable-next=import-outside-toplevel
    from ..bottle_state import is_preserved
    prefix = resource_prefix() + "home-"
    return sorted(v for v in volumes
                  if v.startswith(prefix) and not is_preserved(v[len(prefix):]))


def kept_guests(names: "list[str] | tuple[str, ...]", prefix: str = "") -> set[str]:
    """Among `names` (`<prefix><bottled agent>`), the guests kept for resume:
    a bottled agent kept for resume whose guest is still there was kept by
    its bottle's `preserve_container` (any other is removed at teardown), so
    cleanup leaves it, as it leaves its home and state."""
    # pylint: disable-next=import-outside-toplevel
    from ..bottle_state import is_preserved
    prefix = prefix or resource_prefix()
    return {n for n in names if n.startswith(prefix) and is_preserved(n[len(prefix):])}


__all__ = ["CleanupError", "CleanupFailures", "kept_guests", "orphan_homes"]
