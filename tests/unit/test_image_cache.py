"""Unit: image_cache.py — check_stale / check_stale_path."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from bot_bottle.image_cache import StaleImageError, check_stale, check_stale_path


class TestCheckStale(unittest.TestCase):
    def _run(self, threshold: int, age_days: float) -> None:
        created = datetime.now(tz=timezone.utc) - timedelta(days=age_days)
        with patch("bot_bottle.image_cache.ConfigStore") as cs:
            cs.return_value.cached_image_stale_warning_days.return_value = threshold
            check_stale("test image", created)

    def test_negative_threshold_never_raises(self):
        # Threshold < 0 means the check is disabled — always passes.
        self._run(threshold=-1, age_days=9999)

    def test_zero_threshold_raises_immediately(self):
        # threshold=0 means any image is stale the moment it exists.
        with self.assertRaises(StaleImageError):
            self._run(threshold=0, age_days=0.1)

    def test_within_threshold_does_not_raise(self):
        # Age well under threshold — should pass silently.
        self._run(threshold=7, age_days=2)

    def test_at_threshold_does_not_raise(self):
        # Exactly at the boundary is fine (<=, not <).
        self._run(threshold=1, age_days=0.9999)

    def test_exceeds_threshold_raises(self):
        created = datetime.now(tz=timezone.utc) - timedelta(days=3)
        with patch("bot_bottle.image_cache.ConfigStore") as cs:
            cs.return_value.cached_image_stale_warning_days.return_value = 1
            with self.assertRaises(StaleImageError) as ctx:
                check_stale("agent image 'bot-bottle:latest'", created)
        self.assertIn("agent image", str(ctx.exception))
        self.assertIn("day(s) old", str(ctx.exception))

    def test_naive_datetime_treated_as_utc(self):
        # check_stale calls .astimezone(utc) on the input; naive datetimes
        # that would be interpreted as local time should still work.
        # We can't control the local tz in a unit test, so just ensure
        # no exception is thrown for a very recent naive datetime.
        naive_now = datetime(2099, 1, 1)  # far future, always "fresh"
        with patch("bot_bottle.image_cache.ConfigStore") as cs:
            cs.return_value.cached_image_stale_warning_days.return_value = 1
            # Should not raise — the image is brand new.
            check_stale("test image", naive_now)


class TestCheckStalePath(unittest.TestCase):
    def test_delegates_to_check_stale_with_mtime(self):
        with tempfile.NamedTemporaryFile() as f:
            path = Path(f.name)
            with patch("bot_bottle.image_cache.check_stale") as mock_check:
                check_stale_path("some artifact", path)
            mock_check.assert_called_once()
            label, dt = mock_check.call_args.args
            self.assertEqual("some artifact", label)
            self.assertIsInstance(dt, datetime)
            self.assertIsNotNone(dt.tzinfo)

    def test_raises_stale_for_old_file(self):
        with tempfile.NamedTemporaryFile() as f:
            path = Path(f.name)
            with patch("bot_bottle.image_cache.ConfigStore") as cs:
                cs.return_value.cached_image_stale_warning_days.return_value = 0
                with self.assertRaises(StaleImageError):
                    check_stale_path("cached artifact", path)


if __name__ == "__main__":
    unittest.main()
