"""Shared helpers for cached-image quickstart stale checks."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .store.config_store import ConfigStore


class StaleImageError(Exception):
    """Raised when a cached image or artifact exceeds the configured staleness
    threshold. Callers can catch this to prompt interactively; headless paths
    let it propagate as a fatal error."""


def check_stale(label: str, created_at: datetime) -> None:
    """Raise StaleImageError if `created_at` is older than the configured
    stale-warning threshold. Negative threshold disables the check."""
    threshold_days = ConfigStore().cached_image_stale_warning_days()
    if threshold_days < 0:
        return
    now = datetime.now(timezone.utc)
    created = created_at.astimezone(timezone.utc)
    age = now - created
    if age.total_seconds() <= threshold_days * 86400:
        return
    raise StaleImageError(
        f"cached {label} is {age.days} day(s) old; "
        "quickstart does not verify it matches the current Dockerfile/context"
    )


def check_stale_path(label: str, path: Path) -> None:
    """Raise StaleImageError if `path`'s mtime exceeds the staleness threshold."""
    check_stale(label, datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))


__all__ = ["StaleImageError", "check_stale", "check_stale_path"]
