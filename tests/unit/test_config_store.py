"""Unit tests for the host-side configuration store."""

from __future__ import annotations

import gc
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.lib.config_store import (
    DEFAULT_CACHED_IMAGE_STALE_WARNING_DAYS,
    AuditForward,
    ConfigStore,
    FirehoseSettings,
    FirehoseSink,
)
from bot_bottle.orchestrator.store.store_manager import StoreManager


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
                "audit_delivery",
                "audit_forward_url",
                "audit_forward_secret",
                "audit_forward_format",
                "firehose_sinks",
                "firehose_request_bodies",
                "firehose_response_bodies",
                "firehose_max_body_bytes",
                "transcript_retention_days",
                "host_max_disk_gb",
                "host_max_memory_gb",
            ], columns)

    def test_firehose_settings_round_trip(self) -> None:
        with tempfile.TemporaryDirectory(prefix="config-store.") as tmp:
            store = ConfigStore(Path(tmp) / "bot-bottle.db")
            self.assertEqual(FirehoseSettings(), store.firehose())  # no DB yet
            store.migrate()
            settings = FirehoseSettings(
                sinks=(FirehoseSink("exec", command=("vector", "--config", "x.toml")),
                       FirehoseSink("https", url="https://siem.example/in", secret="SIEM")),
                request_bodies=False, response_bodies=True, max_body_bytes=1024)
            store.set_firehose(settings)
            self.assertEqual(settings, store.firehose())

    def test_audit_forward_round_trips_and_clears(self) -> None:
        with tempfile.TemporaryDirectory(prefix="config-store.") as tmp:
            store = ConfigStore(Path(tmp) / "bot-bottle.db")
            store.migrate()
            self.assertIsNone(store.audit_forward())
            forward = AuditForward(url="https://siem.example/in", secret="SIEM_TOKEN",
                                   format="ocsf")
            store.set_audit_forward(forward)
            self.assertEqual(forward, store.audit_forward())
            store.set_audit_forward(None)
            self.assertIsNone(store.audit_forward())

    def test_audit_delivery_defaults_to_best_effort_and_can_be_set(self) -> None:
        with tempfile.TemporaryDirectory(prefix="config-store.") as tmp:
            store = ConfigStore(Path(tmp) / "bot-bottle.db")
            self.assertEqual("best_effort", store.audit_delivery())  # no DB yet
            store.migrate()
            self.assertEqual("best_effort", store.audit_delivery())
            store.set_audit_delivery("required")
            self.assertEqual("required", store.audit_delivery())

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


class TestConnectionsClose(unittest.TestCase):
    """Each read and write closes its connection. `with conn:` only commits,
    so a store that used it left one open per call until garbage collection,
    and a busy host controller ran out of file descriptors (EMFILE)."""

    @staticmethod
    def _open_on(path: Path) -> int:
        fd_dir = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
        count = 0
        for name in os.listdir(fd_dir):
            try:
                if os.readlink(os.path.join(fd_dir, name)) == str(path):
                    count += 1
            except OSError:
                continue
        return count

    def test_repeated_calls_leave_no_connection_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = (Path(tmp) / "bot-bottle.db").resolve()
            store = ConfigStore(path)
            store.migrate()
            gc.collect()
            gc.disable()
            try:
                for _ in range(50):
                    store.host_budget()
                    store.firehose()
                    store.set_host_budget(store.host_budget())
                self.assertEqual(0, self._open_on(path))
            finally:
                gc.enable()
def _open_fd_count() -> int:
    """Open descriptors in this process (Linux /proc, macOS /dev/fd)."""
    fd_dir = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    return len(os.listdir(fd_dir))


class TestConfigStoreClosesConnections(unittest.TestCase):
    """The host controller polls these every few seconds; an unclosed
    connection per call ran it out of descriptors (EMFILE)."""

    def _store(self, tmp: str) -> ConfigStore:
        store = ConfigStore(Path(tmp) / "bot-bottle.db")
        store.migrate()
        return store

    def test_every_method_closes_its_connection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="config-store.") as tmp:
            store = self._store(tmp)
            calls = [
                store.cached_image_stale_warning_days,
                store.audit_delivery,
                store.audit_forward,
                store.firehose,
                store.transcript_retention_days,
                store.host_budget,
                lambda: store.set_audit_forward(None),
                lambda: store.set_firehose(store.firehose()),
                lambda: store.set_transcript_retention_days(30),
                lambda: store.set_host_budget(store.host_budget()),
                lambda: store.set_audit_delivery("best_effort"),
                lambda: store.set_cached_image_stale_warning_days(1),
            ]
            real_connect = sqlite3.connect
            opened: list[sqlite3.Connection] = []

            def tracking_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
                conn = real_connect(*args, **kwargs)
                opened.append(conn)
                return conn

            for call in calls:
                opened.clear()
                with mock.patch("sqlite3.connect", side_effect=tracking_connect):
                    call()
                self.assertTrue(opened)
                for conn in opened:
                    with self.assertRaises(sqlite3.ProgrammingError):
                        conn.execute("SELECT 1")  # closed

    def test_repeated_polls_do_not_grow_open_descriptors(self) -> None:
        with tempfile.TemporaryDirectory(prefix="config-store.") as tmp:
            store = self._store(tmp)
            gc.disable()  # a leak must not hide behind the cyclic collector
            try:
                before = _open_fd_count()
                for _ in range(200):
                    store.audit_forward()
                    store.audit_delivery()
                    store.firehose()
                after = _open_fd_count()
            finally:
                gc.enable()
            self.assertLessEqual(after - before, 5)


if __name__ == "__main__":
    unittest.main()
