"""Unit tests for the host-side configuration store."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot_bottle.config_store import (
    CACHED_IMAGE_STALE_WARNING_DAYS,
    DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS,
    ConfigStore,
)
from bot_bottle.store_manager import StoreManager


class TestConfigStore(unittest.TestCase):
    def test_cached_image_warning_days_defaults_to_one(self) -> None:
        with tempfile.TemporaryDirectory(prefix="config-store.") as tmp:
            store = ConfigStore(Path(tmp) / "bot-bottle.db")
            store.migrate()
            self.assertEqual(
                DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS,
                store.cached_image_stale_warning_days(),
            )

    def test_cached_image_warning_days_reads_value(self) -> None:
        with tempfile.TemporaryDirectory(prefix="config-store.") as tmp:
            store = ConfigStore(Path(tmp) / "bot-bottle.db")
            store.migrate()
            store.set(CACHED_IMAGE_STALE_WARNING_DAYS, "7")
            self.assertEqual(7, store.cached_image_stale_warning_days())

    def test_store_manager_includes_config_store(self) -> None:
        with tempfile.TemporaryDirectory(prefix="config-store.") as tmp:
            db = Path(tmp) / "bot-bottle.db"
            manager = StoreManager(db)
            self.assertFalse(manager.is_migrated())
            manager.migrate()
            self.assertTrue(manager.is_migrated())


if __name__ == "__main__":
    unittest.main()
