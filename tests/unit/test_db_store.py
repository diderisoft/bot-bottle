"""Unit: DbStore._connection() context manager and is_migrated()."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from bot_bottle.store.db_store import DbStore
from bot_bottle.store.migrations import TableMigrations


def _store(tmp: Path) -> DbStore:
    migrations = TableMigrations("test", ["CREATE TABLE items (id INTEGER PRIMARY KEY)"])
    return DbStore(tmp / "test.db", migrations)


class TestDbStoreIsMigrated(unittest.TestCase):
    def test_returns_false_when_db_absent(self):
        with tempfile.TemporaryDirectory() as d:
            store = _store(Path(d))
            self.assertFalse(store.is_migrated())

    def test_returns_false_when_schema_versions_missing(self):
        # DB file exists but has no schema_versions table → OperationalError → False.
        with tempfile.TemporaryDirectory() as d:
            store = _store(Path(d))
            conn = sqlite3.connect(store.db_path)
            conn.close()
            self.assertFalse(store.is_migrated())

    def test_returns_true_after_migrate(self):
        with tempfile.TemporaryDirectory() as d:
            store = _store(Path(d))
            store.migrate()
            self.assertTrue(store.is_migrated())

    def test_returns_false_when_behind(self):
        with tempfile.TemporaryDirectory() as d:
            migrations = TableMigrations(
                "test",
                [
                    "CREATE TABLE items (id INTEGER PRIMARY KEY)",
                    "ALTER TABLE items ADD COLUMN name TEXT",
                ],
            )
            store = DbStore(Path(d) / "test.db", migrations)
            # Apply only the first migration manually.
            conn = sqlite3.connect(store.db_path)
            with conn:
                TableMigrations("test", [migrations.migrations[0]]).apply(conn)
            conn.close()
            self.assertFalse(store.is_migrated())


if __name__ == "__main__":
    unittest.main()
