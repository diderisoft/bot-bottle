"""Shared helpers for cached-image quickstart warnings."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

try:
    from .config_store import ConfigStore
    from .log import warn
except ImportError:
    from config_store import ConfigStore  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module
    from log import warn  # type: ignore[import-not-found]  # pylint: disable=import-error,no-name-in-module


def warn_if_stale(label: str, created_at: datetime) -> None:
    threshold_days = ConfigStore().cached_image_stale_warning_days()
    if threshold_days < 0:
        return
    now = datetime.now(timezone.utc)
    created = created_at.astimezone(timezone.utc)
    age = now - created
    if age.total_seconds() <= threshold_days * 86400:
        return
    warn(
        f"cached {label} is {age.days} day(s) old; "
        "quickstart does not verify it matches the current Dockerfile/context"
    )


def warn_if_stale_path(label: str, path: Path) -> None:
    warn_if_stale(
        label,
        datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
    )


__all__ = ["warn_if_stale", "warn_if_stale_path"]
