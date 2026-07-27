"""Shared destructive-cleanup execution and failure accounting."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path


class CleanupError(RuntimeError):
    """One or more approved cleanup mutations did not complete."""


class CleanupFailures:
    """Attempt every approved mutation, then fail with complete diagnostics."""

    def __init__(self) -> None:
        self._messages: list[str] = []

    def run(self, argv: Sequence[str], description: str) -> None:
        try:
            result = subprocess.run(
                list(argv), capture_output=True, text=True, check=False,
            )
        except OSError as exc:
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

    def raise_if_any(self) -> None:
        if self._messages:
            raise CleanupError("; ".join(self._messages))


__all__ = ["CleanupError", "CleanupFailures"]
