"""Unit: the host DB (`control/hostctl/host_db.py`), the index DB
(`index_db.py`) and the orchestrator's `registry.db` keep separate tables
(ADR 0020), and every table is marked portable or machine-specific. The host
controller checks the host DB at start, keeps daily backups, and `host
restore-db` puts one back. A missing or damaged index DB is rebuilt."""

from __future__ import annotations

import io
import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bot_bottle.cli.commands import host as host_cmd
from bot_bottle.control.hostctl import host_db, index_db, runner, service_unit
from bot_bottle.lib.config_store import ConfigStore
from bot_bottle.lib.db_store import MACHINE_SPECIFIC, PORTABLE, DbStore
from bot_bottle.orchestrator.store.store_manager import StoreManager
from bot_bottle.paths import host_audit_dir, host_db_path, index_db_path

_INTERNAL = {"schema_versions", "sqlite_sequence"}


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return names - _INTERNAL


def _marked(stores: list[DbStore] | list[index_db.DerivedIndex]) -> dict[str, str]:
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
                     "supervise_audit_entries", "orchestrator_config", "audit_events",
                     "audit_index_cursor"):
            self.assertNotIn(name, tables)

    def test_an_older_db_is_migrated_without_asking(self) -> None:
        host_db_path().parent.mkdir(parents=True, mode=0o700)
        with sqlite3.connect(host_db_path()) as db:
            db.execute("CREATE TABLE something_older (x INTEGER)")
        with redirect_stderr(io.StringIO()):
            host_db.migrate()
        self.assertTrue(all(store.is_migrated() for store in host_db.stores()))

    def test_migrate_drops_nothing_a_host_store_owns(self) -> None:
        self.assertEqual([], host_db.migrate())
        ConfigStore(host_db_path()).set_audit_delivery("required")
        self.assertEqual([], host_db.migrate())
        self.assertEqual(set(_marked(host_db.stores())), _tables(host_db_path()))
        self.assertEqual("required", ConfigStore(host_db_path()).audit_delivery())

    def test_migrate_drops_what_an_older_version_left(self) -> None:
        host_db_path().parent.mkdir(parents=True, mode=0o700)
        with sqlite3.connect(host_db_path()) as db:
            # An older host DB: its audit index, and on Docker the
            # orchestrator's tables, an encrypted secret among them.
            db.execute("CREATE TABLE audit_events (id INTEGER PRIMARY KEY, raw TEXT)")
            db.execute("CREATE INDEX audit_events_by_raw ON audit_events (raw)")
            db.execute("CREATE TABLE bottled_agent_secrets (bottle TEXT, ciphertext BLOB)")
            db.execute("INSERT INTO bottled_agent_secrets VALUES ('b', ?)",
                       (b"OLD-CIPHERTEXT" * 400,))
            db.execute("CREATE TABLE schema_versions (module TEXT PRIMARY KEY, "
                       "version INTEGER NOT NULL DEFAULT 0)")
            db.execute("INSERT INTO schema_versions VALUES ('orchestrator_registry', 3)")
        with redirect_stderr(io.StringIO()):
            host_db.migrate()
        ConfigStore(host_db_path()).set_audit_delivery("required")
        dropped = host_db.migrate()
        self.assertEqual([], dropped)  # dropped on the first start, nothing after
        self.assertEqual(set(_marked(host_db.stores())), _tables(host_db_path()))
        with sqlite3.connect(host_db_path()) as db:
            indexes = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'")}
            modules = {r[0] for r in db.execute("SELECT module FROM schema_versions")}
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertNotIn("audit_events_by_raw", indexes)
        self.assertEqual({s.schema_key for s in host_db.stores()}, modules)
        self.assertEqual("required", ConfigStore(host_db_path()).audit_delivery())
        # VACUUMed: the ciphertext isn't left in a free page.
        self.assertNotIn(b"OLD-CIPHERTEXT", host_db_path().read_bytes())

    def test_migrate_says_what_it_dropped(self) -> None:
        host_db_path().parent.mkdir(parents=True, mode=0o700)
        with sqlite3.connect(host_db_path()) as db:
            db.execute("CREATE TABLE orchestrator_bottles (id TEXT)")
            db.execute("CREATE TABLE audit_index_cursor (id INTEGER)")
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(["audit_index_cursor", "orchestrator_bottles"], host_db.migrate())

    def test_the_first_backup_after_an_upgrade_holds_none_of_it(self) -> None:
        host_db_path().parent.mkdir(parents=True, mode=0o700)
        with sqlite3.connect(host_db_path()) as db:
            db.execute("CREATE TABLE bottled_agent_secrets (bottle TEXT, ciphertext BLOB)")
        with redirect_stderr(io.StringIO()):
            self.assertTrue(host_db.open_for_start())
        backup = host_db.backups()[0].path
        self.assertNotIn("bottled_agent_secrets", _tables(backup))


class TestIndexDb(_Root):
    """`index.db`: derived, so a missing or damaged one is rebuilt, never
    restored or backed up."""

    def journal(self) -> None:
        # pylint: disable=import-outside-toplevel
        from bot_bottle.contract.audit import AuditEvent
        from bot_bottle.control.hostctl.audit_events import HostAudit
        audit = HostAudit.open()
        audit.record(AuditEvent(type="egress.decision", actor="bottled-agent:a",
                                action="egress.connect", outcome="blocked",
                                sensitivity="security", ts_event="2026-11-01T09:30:00.000Z",
                                bottle="amber-fox"))
        audit.close()

    def start(self) -> tuple[bool, str]:
        err = io.StringIO()
        with redirect_stderr(err):
            rebuilt = index_db.open_for_start()
            if rebuilt:
                index_db.rebuild()
        return rebuilt, err.getvalue()

    def indexed(self) -> int:
        with sqlite3.connect(index_db_path()) as db:
            return int(db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])

    def test_a_missing_index_db_is_created_and_rebuilt_from_the_journal(self) -> None:
        self.journal()
        self.assertTrue(self.start()[0])
        self.assertEqual({"audit_events", "audit_index_cursor"}, _tables(index_db_path()))
        self.assertEqual(0o600, stat.S_IMODE(index_db_path().stat().st_mode))
        self.assertTrue(list(host_audit_dir().iterdir()))
        self.assertGreater(self.indexed(), 0)

    def test_a_sound_index_db_is_kept(self) -> None:
        self.journal()
        self.start()
        self.assertEqual((False, ""), self.start())

    def test_a_damaged_index_db_is_deleted_and_rebuilt(self) -> None:
        self.journal()
        self.start()
        count = self.indexed()
        with sqlite3.connect(index_db_path()) as db:
            db.execute("CREATE TABLE filler (x TEXT)")
            db.executemany("INSERT INTO filler VALUES (?)", [("x" * 500,)] * 100)
        _corrupt(index_db_path())
        rebuilt, err = self.start()
        self.assertTrue(rebuilt)
        self.assertIn("failed its check", err)
        self.assertEqual(count, self.indexed())
        self.assertNotIn("filler", _tables(index_db_path()))

    def test_the_start_checks_it_in_the_background_and_readers_wait(self) -> None:
        self.journal()
        release, started = threading.Event(), threading.Event()
        real = index_db.open_for_start

        def slow(db_path: Path | None = None) -> bool:
            started.set()
            release.wait(10)
            return real(db_path)

        got: list[object] = []
        with mock.patch.object(index_db, "open_for_start", slow), \
                redirect_stderr(io.StringIO()):
            opening = index_db.open_in_background()
            self.assertTrue(started.wait(10))
            reader = threading.Thread(target=lambda: got.append(index_db.audit_index()))
            reader.start()
            reader.join(0.2)
            self.assertEqual([], got)  # waiting on the check
            release.set()
            reader.join(10)
            opening.join(10)
        self.assertEqual(1, len(got))
        self.assertGreater(self.indexed(), 0)  # rebuilt, the file was missing

    def test_a_table_no_index_owns_is_dropped(self) -> None:
        self.journal()
        self.start()
        with sqlite3.connect(index_db_path()) as db:
            db.execute("CREATE TABLE an_old_index (x TEXT)")
        self.assertEqual((False, ""), self.start())
        self.assertEqual({"audit_events", "audit_index_cursor"}, _tables(index_db_path()))
        self.assertGreater(self.indexed(), 0)

    def test_it_isnt_backed_up_with_the_host_db(self) -> None:
        host_db.migrate()
        self.start()
        backup = host_db.back_up(now=_T0)
        self.assertNotIn("audit_events", _tables(backup))
        self.assertEqual([backup], list(host_db.backups_dir().iterdir()))


class TestRegistryTables(_Root):
    def test_the_orchestrator_db_holds_only_its_own_tables(self) -> None:
        path = self.base / "db" / "registry.db"
        manager = StoreManager(path)
        manager.migrate()
        self.assertTrue(manager.is_migrated())
        self.assertEqual(set(_marked(manager.stores())), _tables(path))
        self.assertNotIn("bot_bottle_config", _tables(path))
        self.assertEqual([], manager.migrate())
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE supervise_audit_entries (id INTEGER)")
        self.assertEqual(["supervise_audit_entries"], manager.migrate())
        self.assertEqual(set(_marked(manager.stores())), _tables(path))


class TestEveryTableIsMarked(_Root):
    def test_each_store_marks_exactly_the_tables_it_creates(self) -> None:
        stores = [*host_db.stores(self.base / "h" / "host.db"),
                  *StoreManager(self.base / "o" / "registry.db").stores()]
        indexes = index_db.stores(self.base / "i" / "index.db")
        for store in [*stores, *indexes]:
            store.migrate()
        made = (_tables(self.base / "h" / "host.db") | _tables(self.base / "o" / "registry.db")
                | _tables(self.base / "i" / "index.db"))
        marks = _marked(stores) | _marked(indexes)
        self.assertEqual(made, set(marks))
        self.assertTrue(set(marks.values()) <= {PORTABLE, MACHINE_SPECIFIC})

    def test_settings_forges_and_passkeys_are_portable_and_the_rest_machine_specific(
            self) -> None:
        marks = _marked([*host_db.stores(self.base / "host.db"),
                         *StoreManager(self.base / "registry.db").stores()])
        marks |= _marked(index_db.stores(self.base / "index.db"))
        self.assertEqual({"bot_bottle_config", "forge_connections", "forge_users",
                          "audit_passkeys"},
                         {name for name, mark in marks.items() if mark == PORTABLE})


_T0 = datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc)


def _corrupt(path: Path) -> None:
    """Scribble over the DB's pages past its header, as a bad disk would."""
    with path.open("r+b") as fh:
        fh.seek(4096)
        fh.write(b"\xff" * 8192)


class _HostDb(_Root):
    def setUp(self) -> None:
        super().setUp()
        host_db.migrate()
        store = ConfigStore(host_db_path())
        store.set_audit_delivery("required")
        # Enough rows to span several pages.
        with sqlite3.connect(host_db_path()) as db:
            db.execute("CREATE TABLE filler (x TEXT)")
            db.executemany("INSERT INTO filler VALUES (?)", [("x" * 500,)] * 100)


class TestCheck(_HostDb):
    def test_a_sound_db_and_a_missing_one_pass(self) -> None:
        self.assertEqual("", host_db.check())
        self.assertEqual("", host_db.check(self.base / "nothing.db"))

    def test_a_damaged_db_fails(self) -> None:
        _corrupt(host_db_path())
        self.assertNotEqual("", host_db.check())

    def test_a_file_that_isnt_a_db_fails(self) -> None:
        host_db_path().write_bytes(b"not a database at all, just bytes" * 200)
        self.assertNotEqual("", host_db.check())


class TestBackups(_HostDb):
    def test_a_backup_is_a_private_consistent_copy(self) -> None:
        path = host_db.back_up(now=_T0)
        self.assertEqual(host_db.backups_dir() / "bot-bottle-20261101T093000Z.db", path)
        self.assertEqual(0o700, stat.S_IMODE(host_db.backups_dir().stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual("", host_db.check(path))
        self.assertEqual("required", ConfigStore(path).audit_delivery())
        self.assertEqual([host_db.Backup(path, _T0)], host_db.backups())

    def test_only_the_newest_three_are_kept(self) -> None:
        for day in range(5):
            host_db.back_up(now=_T0 + timedelta(days=day))
        kept = host_db.backups()
        self.assertEqual([_T0 + timedelta(days=d) for d in (4, 3, 2)], [b.taken for b in kept])
        self.assertEqual(3, len(list(host_db.backups_dir().iterdir())))

    def test_one_a_day(self) -> None:
        self.assertIsNotNone(host_db.back_up_if_due(now=_T0))
        self.assertIsNone(host_db.back_up_if_due(now=_T0 + timedelta(hours=23)))
        self.assertIsNotNone(host_db.back_up_if_due(now=_T0 + timedelta(hours=24)))
        self.assertEqual(2, len(host_db.backups()))

    def test_nothing_to_back_up_before_the_db_exists(self) -> None:
        self.assertIsNone(host_db.back_up_if_due(self.base / "new" / "bot-bottle.db"))


class TestDailyBackups(unittest.TestCase):
    def test_stop_waits_for_a_backup_in_progress(self) -> None:
        backing_up, release = threading.Event(), threading.Event()
        done: list[bool] = []

        def slow() -> None:
            backing_up.set()
            release.wait(10)
            done.append(True)

        with mock.patch.object(host_db, "back_up_if_due", slow):
            daily = host_db.DailyBackups(interval=0.01).start()
            self.assertTrue(backing_up.wait(10))
            threading.Timer(0.2, release.set).start()
            daily.stop()
        self.assertEqual([True], done)


class TestStart(_HostDb):
    def start(self) -> tuple[bool, str]:
        err = io.StringIO()
        with redirect_stderr(err):
            ok = host_db.open_for_start()
        return ok, err.getvalue()

    def test_a_sound_db_starts_backed_up_and_migrated(self) -> None:
        # `filler` is no store's, so it goes.
        self.assertEqual((True, "bot-bottle: host database: dropped tables no host store "
                                "owns: filler\n"), self.start())
        self.assertEqual(1, len(host_db.backups()))
        self.assertTrue(all(store.is_migrated() for store in host_db.stores()))

    def test_a_failed_check_refuses_naming_the_newest_backup(self) -> None:
        host_db.back_up(now=_T0)
        host_db.back_up(now=_T0 + timedelta(days=1))
        _corrupt(host_db_path())
        ok, err = self.start()
        self.assertFalse(ok)
        self.assertIn("failed its integrity check", err)
        self.assertIn("bot-bottle-20261102T093000Z.db, taken 2026-11-02 09:30 UTC", err)
        self.assertIn("bot-bottle host restore-db", err)
        self.assertEqual(1, len(err.strip().splitlines()))  # the CLI shows the log's last line

    def test_with_no_backup_it_says_so(self) -> None:
        _corrupt(host_db_path())
        ok, err = self.start()
        self.assertFalse(ok)
        self.assertIn("no backup", err)

    def test_the_service_unit_doesnt_restart_a_refusal(self) -> None:
        spec = service_unit.ServiceSpec(python="/usr/bin/python3", path_env="/usr/bin")
        self.assertIn(f"RestartPreventExitStatus={runner.REFUSED_EXIT_STATUS}",
                      service_unit.render_systemd(spec))


class TestRestore(_HostDb):
    def test_restores_the_newest_and_keeps_the_db_it_replaces(self) -> None:
        old = host_db.back_up(now=_T0)
        ConfigStore(host_db_path()).set_audit_delivery("best_effort")
        newest = host_db.back_up(now=_T0 + timedelta(days=1))
        ConfigStore(host_db_path()).set_audit_delivery("required")
        _corrupt(host_db_path())
        restored = host_db.restore()
        self.assertEqual(newest, restored.backup)
        self.assertEqual("", host_db.check())
        self.assertEqual("best_effort", ConfigStore(host_db_path()).audit_delivery())
        self.assertEqual(0o600, stat.S_IMODE(host_db_path().stat().st_mode))
        assert restored.replaced is not None
        self.assertTrue(restored.replaced.is_file())
        self.assertTrue(newest.is_file() and old.is_file())  # backups stay

    def test_restores_a_named_backup(self) -> None:
        old = host_db.back_up(now=_T0)
        ConfigStore(host_db_path()).set_audit_delivery("best_effort")
        host_db.back_up(now=_T0 + timedelta(days=1))
        self.assertEqual(old, host_db.restore(old).backup)
        self.assertEqual("required", ConfigStore(host_db_path()).audit_delivery())

    def test_the_old_wal_goes_with_the_old_db(self) -> None:
        backup = host_db.back_up(now=_T0)
        wal = host_db_path().with_name("bot-bottle.db-wal")
        wal.write_bytes(b"old wal")
        restored = host_db.restore(backup)
        self.assertFalse(wal.exists())
        assert restored.replaced is not None
        self.assertEqual(b"old wal",
                         restored.replaced.with_name(restored.replaced.name + "-wal").read_bytes())

    def test_refuses_without_a_sound_backup(self) -> None:
        with self.assertRaises(host_db.HostDbError):
            host_db.restore()
        backup = host_db.back_up(now=_T0)
        _corrupt(backup)
        with self.assertRaises(host_db.HostDbError):
            host_db.restore(backup)
        self.assertEqual("", host_db.check())  # the DB was left alone


class TestRestoreCommand(_HostDb):
    def run_cmd(self, *argv: str, running: bool = False) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        state = runner.HostControlState(pid=1, port=1, build_id="b") if running else None
        with mock.patch.object(runner, "running", return_value=state), \
                redirect_stdout(out), redirect_stderr(err):
            code = host_cmd.cmd_host(["restore-db", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_refuses_while_the_host_controller_runs(self) -> None:
        host_db.back_up(now=_T0)
        code, _, err = self.run_cmd(running=True)
        self.assertEqual(1, code)
        self.assertIn("host controller is running", err)

    def test_lists_then_restores_by_name(self) -> None:
        host_db.back_up(now=_T0)
        host_db.back_up(now=_T0 + timedelta(days=1))
        code, out, _ = self.run_cmd("--list")
        self.assertEqual(0, code)
        self.assertEqual(["bot-bottle-20261102T093000Z.db  taken 2026-11-02 09:30 UTC",
                          "bot-bottle-20261101T093000Z.db  taken 2026-11-01 09:30 UTC"],
                         out.splitlines())
        code, out, _ = self.run_cmd("bot-bottle-20261101T093000Z.db")
        self.assertEqual(0, code)
        self.assertIn("restored bot-bottle-20261101T093000Z.db", out)
        self.assertIn("replaced is kept at", out)

    def test_says_when_there_is_nothing_to_restore(self) -> None:
        code, _, err = self.run_cmd()
        self.assertEqual(1, code)
        self.assertIn("no backups", err)


class TestDaemonLock(_Root):
    """The host controller's lifetime lock: two never run for one root."""

    def test_one_holder_at_a_time_naming_the_first(self) -> None:
        fd = runner.hold_daemon_lock()
        try:
            with self.assertRaisesRegex(runner.DaemonLockHeld, f"pid {os.getpid()}"):
                runner.hold_daemon_lock()
        finally:
            os.close(fd)
        os.close(runner.hold_daemon_lock())  # free again

    def test_a_second_host_controller_exits_before_touching_anything(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.control.hostctl import __main__ as daemon
        fd = runner.hold_daemon_lock()
        cwd = os.getcwd()
        socket_path = runner.control_socket_path()
        socket_path.write_text("a live daemon's socket")
        try:
            with mock.patch.object(daemon, "LogRotator") as rotator, \
                    mock.patch.object(daemon.host_db, "open_for_start") as opened, \
                    redirect_stderr(io.StringIO()) as err:
                self.assertEqual(runner.REFUSED_EXIT_STATUS, daemon.main())
        finally:
            os.close(fd)
            os.chdir(cwd)
        rotator.assert_not_called()
        opened.assert_not_called()
        self.assertTrue(socket_path.exists())
        self.assertIn("not starting", err.getvalue())


if __name__ == "__main__":
    unittest.main()
