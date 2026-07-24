"""Unit tests for the host-side configuration store."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from bot_bottle.store.config_store import (
    DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS,
    ConfigStore,
)
from bot_bottle.store.store_manager import StoreManager


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

    def test_cached_image_warning_days_returns_default_when_db_missing(self) -> None:
        # When the db file doesn't exist yet (parent exists, file doesn't),
        # the store returns the default without touching the file.
        with tempfile.TemporaryDirectory(prefix="config-store.") as tmp:
            store = ConfigStore(Path(tmp) / "missing.db")
            self.assertEqual(
                DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS,
                store.cached_image_stale_warning_days(),
            )

    def test_cached_image_warning_days_returns_default_on_null_value(self) -> None:
        # If the row exists but the value is NULL (or not castable to int),
        # the store falls back to the default.
        with tempfile.TemporaryDirectory(prefix="config-store.") as tmp:
            db_path = Path(tmp) / "bot-bottle.db"
            store = ConfigStore(db_path)
            store.migrate()
            # Write a NULL value directly.
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE bot_bottle_config SET cached_image_stale_warning_days = NULL WHERE id = 1"
                )
            self.assertEqual(
                DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS,
                store.cached_image_stale_warning_days(),
            )


if __name__ == "__main__":
    unittest.main()
