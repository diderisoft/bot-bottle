"""Unit tests for the host-side configuration store."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from bot_bottle.config_store import (
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
            store.set_cached_image_stale_warning_days(7)
            self.assertEqual(7, store.cached_image_stale_warning_days())

    def test_config_schema_uses_explicit_settings_columns(self) -> None:
        with tempfile.TemporaryDirectory(prefix="config-store.") as tmp:
            store = ConfigStore(Path(tmp) / "bot-bottle.db")
            store.migrate()
            with sqlite3.connect(store.db_path) as conn:
                conn.row_factory = sqlite3.Row
                columns = [
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(bot_bottle_config)")
                ]
            self.assertEqual([
                "id",
                "cached_image_stale_warning_days",
            ], columns)

    def test_store_manager_includes_config_store(self) -> None:
        with tempfile.TemporaryDirectory(prefix="config-store.") as tmp:
            db = Path(tmp) / "bot-bottle.db"
            manager = StoreManager(db)
            self.assertFalse(manager.is_migrated())
            manager.migrate()
            self.assertTrue(manager.is_migrated())


if __name__ == "__main__":
    unittest.main()
