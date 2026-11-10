"""Unit: the host DB (`control/hostctl/host_db.py`) and the orchestrator's
`registry.db` keep separate tables (ADR 0020), and every table is marked
portable or machine-specific."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.control.hostctl import host_db
from bot_bottle.lib.db_store import MACHINE_SPECIFIC, PORTABLE, DbStore
from bot_bottle.orchestrator.store.store_manager import StoreManager
from bot_bottle.paths import host_db_path

_INTERNAL = {"schema_versions", "sqlite_sequence"}


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return names - _INTERNAL


def _marked(stores: list[DbStore]) -> dict[str, str]:
    return {name: mark for store in stores for name, mark in store.TABLES.items()}


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"HOME": str(self.base / "home"),
                                           "BOT_BOTTLE_ROOT": str(self.base / "root")})
        env.start()
        self.addCleanup(env.stop)


class TestHostTables(_Root):
    def test_migrate_creates_every_host_table_and_no_orchestrator_one(self) -> None:
        host_db.migrate()
        tables = _tables(host_db_path())
        self.assertEqual(set(_marked(host_db.stores())), tables)
        orchestrator = set(_marked(StoreManager(self.base / "registry.db").stores()))
        self.assertEqual(set(), tables & orchestrator)
        for name in ("orchestrator_bottles", "supervise_proposals", "canary_events",
                     "supervise_audit_entries", "orchestrator_config"):
            self.assertNotIn(name, tables)

    def test_an_older_db_is_migrated_without_asking(self) -> None:
        host_db_path().parent.mkdir(parents=True, mode=0o700)
        with sqlite3.connect(host_db_path()) as db:
            db.execute("CREATE TABLE something_older (x INTEGER)")
        host_db.migrate()
        self.assertTrue(all(store.is_migrated() for store in host_db.stores()))


class TestRegistryTables(_Root):
    def test_the_orchestrator_db_holds_only_its_own_tables(self) -> None:
        path = self.base / "db" / "registry.db"
        manager = StoreManager(path)
        manager.migrate()
        self.assertTrue(manager.is_migrated())
        self.assertEqual(set(_marked(manager.stores())), _tables(path))
        self.assertNotIn("bot_bottle_config", _tables(path))


class TestEveryTableIsMarked(_Root):
    def test_each_store_marks_exactly_the_tables_it_creates(self) -> None:
        stores = [*host_db.stores(self.base / "h" / "host.db"),
                  *StoreManager(self.base / "o" / "registry.db").stores()]
        for store in stores:
            store.migrate()
        made = _tables(self.base / "h" / "host.db") | _tables(self.base / "o" / "registry.db")
        marks = _marked(stores)
        self.assertEqual(made, set(marks))
        self.assertTrue(set(marks.values()) <= {PORTABLE, MACHINE_SPECIFIC})

    def test_settings_are_portable_and_the_rest_machine_specific(self) -> None:
        marks = _marked([*host_db.stores(self.base / "host.db"),
                         *StoreManager(self.base / "registry.db").stores()])
        self.assertEqual({"bot_bottle_config"},
                         {name for name, mark in marks.items() if mark == PORTABLE})


if __name__ == "__main__":
    unittest.main()
