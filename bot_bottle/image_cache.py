"""Staleness checks for cached images and artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .lib.config_store import ConfigStore
from .paths import host_db_path


class StaleImageError(Exception):
    """A cached image or artifact is older than the staleness threshold."""


def check_stale(label: str, created_at: datetime) -> None:
    """Raise StaleImageError if `created_at` is past the threshold. A negative
    threshold disables the check."""
    threshold_days = ConfigStore(host_db_path()).cached_image_stale_warning_days()
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
    """Raise StaleImageError if `path`'s mtime is past the threshold."""
    check_stale(label, datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))


__all__ = ["StaleImageError", "check_stale", "check_stale_path"]
