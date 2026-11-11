"""Staleness checks for cached images and artifacts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import host_access
from .lib.config_store import DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS, ConfigStore
from .paths import host_db_path


class StaleImageError(Exception):
    """A cached image or artifact is older than the staleness threshold."""


def _threshold_days() -> int:
    """The host's setting, in the host controller, where launches run. Any
    other process keeps to the default rather than open the host DB."""
    if not host_access.in_host_controller():
        return DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS
    return ConfigStore(host_db_path()).cached_image_stale_warning_days()


def check_stale(label: str, created_at: datetime) -> None:
    """Raise StaleImageError if `created_at` is past the threshold. A negative
    threshold disables the check."""
    threshold_days = _threshold_days()
    if threshold_days < 0:
        return
    now = datetime.now(timezone.utc)
    created = created_at.astimezone(timezone.utc)
    age = now - created
    if age <= timedelta(days=threshold_days):
        return
    raise StaleImageError(
        f"cached {label} is {age.days} day(s) old; "
        "quickstart does not verify it matches the current Dockerfile/context"
    )


def check_stale_path(label: str, path: Path) -> None:
    """Raise StaleImageError if `path`'s mtime is past the threshold."""
    check_stale(label, datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))


__all__ = ["StaleImageError", "check_stale", "check_stale_path"]
