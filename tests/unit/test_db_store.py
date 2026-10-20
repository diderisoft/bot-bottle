"""Unit: DbStore._connection() context manager and is_migrated()."""

from __future__ import annotations

import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.lib.db_store import DbStore
from bot_bottle.lib.migrations import TableMigrations


def _store(tmp: Path) -> DbStore:
    migrations = TableMigrations("test", ["CREATE TABLE items (id INTEGER PRIMARY KEY)"])
    return DbStore(tmp / "test.db", migrations)


class TestDbStoreIsMigrated(unittest.TestCase):
    def test_returns_false_when_db_absent(self):
        with tempfile.TemporaryDirectory() as d:
            store = _store(Path(d))
            self.assertFalse(store.is_migrated())

    def test_creates_private_directory_and_database_before_first_open(self):
        with tempfile.TemporaryDirectory() as d:
            parent = Path(d) / "store"
            store = _store(parent)
            self.assertEqual(0o700, stat.S_IMODE(parent.stat().st_mode))
            self.assertFalse(store.db_path.exists())
            store.migrate()
            self.assertEqual(0o600, stat.S_IMODE(store.db_path.stat().st_mode))

    def test_repairs_existing_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            parent = Path(d) / "store"
            parent.mkdir(mode=0o755)
            db_path = parent / "test.db"
            db_path.touch(mode=0o644)
            store = _store(parent)
            self.assertEqual(db_path, store.db_path)
            self.assertEqual(0o700, stat.S_IMODE(parent.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(db_path.stat().st_mode))

    def test_permission_repair_failure_is_not_suppressed(self):
        with tempfile.TemporaryDirectory() as d:
            parent = Path(d) / "store"
            parent.mkdir()
            (parent / "test.db").touch()
            with patch("os.fchmod", side_effect=OSError("denied")):
                with self.assertRaisesRegex(OSError, "denied"):
                    _store(parent)

    def test_rejects_database_symlink_without_changing_target(self):
        with tempfile.TemporaryDirectory() as d:
            parent = Path(d) / "store"
            parent.mkdir()
            target = Path(d) / "target"
            target.touch(mode=0o644)
            (parent / "test.db").symlink_to(target)

            with self.assertRaises(OSError):
                _store(parent)

            self.assertEqual(0o644, stat.S_IMODE(target.stat().st_mode))

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
