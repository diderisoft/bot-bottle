"""Runs as root through sudo leave no per-user state behind (#975), and a user
locked out by state an earlier one left gets told how to take it back."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from bot_bottle import environment as env
from bot_bottle.cli import __main__ as cli_main
from bot_bottle.cli.commands import doctor


class TestSudoInvoker(unittest.TestCase):
    def _invoker(self, euid: int, environ: dict[str, str]) -> tuple[int, int] | None:
        with mock.patch.object(env.os, "geteuid", return_value=euid), \
                mock.patch.dict(os.environ, environ, clear=True):
            return env.sudo_invoker()

    def test_root_through_sudo_is_the_real_user(self) -> None:
        self.assertEqual((501, 20), self._invoker(0, {"SUDO_UID": "501", "SUDO_GID": "20"}))

    def test_not_root_is_none(self) -> None:
        self.assertIsNone(self._invoker(501, {"SUDO_UID": "501", "SUDO_GID": "20"}))

    def test_root_without_sudo_is_none(self) -> None:
        self.assertIsNone(self._invoker(0, {}))

    def test_sudo_from_root_or_garbage_is_none(self) -> None:
        self.assertIsNone(self._invoker(0, {"SUDO_UID": "0", "SUDO_GID": "0"}))
        self.assertIsNone(self._invoker(0, {"SUDO_UID": "me", "SUDO_GID": "20"}))


class TestRootOwnedState(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"BOT_BOTTLE_ENV": "dev", "HOME": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = env.envs_dir(self.home) / "dev"
        self.root.mkdir(parents=True)
        (self.root / "env.json").write_text("{}", encoding="utf-8")
        (self.root / "db").mkdir()

    def _owned_by_root(self, *names: str):  # type: ignore[no-untyped-def]
        """Report `names` as root's and every other path as uid 1000's, whoever
        really owns them (CI runs the tests as root)."""
        real = Path.lstat

        def lstat(path: Path):  # type: ignore[no-untyped-def]
            st = real(path)
            uid = 0 if path.name in names else 1000
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, uid,
                                   st.st_gid, st.st_size, 0, 0, 0))
        return mock.patch.object(Path, "lstat", lstat)

    def test_nothing_root_owned(self) -> None:
        with mock.patch.object(env.os, "geteuid", return_value=1000), self._owned_by_root():
            self.assertEqual([], env.root_owned_state(self.home))

    def test_finds_root_owned_root_and_entries(self) -> None:
        with mock.patch.object(env.os, "geteuid", return_value=1000), \
                self._owned_by_root("dev", "env.json"):
            found = env.root_owned_state(self.home)
        self.assertEqual({self.root, self.root / "env.json"}, set(found))

    def test_finds_a_root_owned_registry_lock(self) -> None:
        (env.envs_dir(self.home) / ".lock").write_text("", encoding="utf-8")
        with mock.patch.object(env.os, "geteuid", return_value=1000), \
                self._owned_by_root(".lock"):
            self.assertEqual([env.envs_dir(self.home) / ".lock"], env.root_owned_state(self.home))

    def test_root_itself_owns_everything(self) -> None:
        with mock.patch.object(env.os, "geteuid", return_value=0), \
                self._owned_by_root("dev", "env.json"):
            self.assertEqual([], env.root_owned_state(self.home))

    def test_a_missing_root_is_fine(self) -> None:
        with mock.patch.dict(os.environ, {"BOT_BOTTLE_ENV": "nope"}), \
                mock.patch.object(env.os, "geteuid", return_value=1000), self._owned_by_root():
            self.assertEqual([], env.root_owned_state(self.home))

    def test_fix_names_every_path_once(self) -> None:
        fix = env.root_owned_fix([Path("/h/b"), Path("/h/a"), Path("/h/b")])
        self.assertEqual('sudo chown -R "$(id -un):$(id -gn)" /h/a /h/b', fix)


class TestMainUnderSudo(unittest.TestCase):
    def _main(self, command: str, *, sudo: tuple[int, int] | None,
              stale: list[Path]) -> tuple[int, mock.MagicMock, mock.MagicMock, str]:
        handler = mock.Mock(return_value=0)
        err = io.StringIO()
        with mock.patch.dict(cli_main.COMMANDS, {command: handler}), \
                mock.patch.object(cli_main, "sudo_invoker", return_value=sudo), \
                mock.patch.object(cli_main, "root_owned_state", return_value=stale), \
                mock.patch.object(cli_main, "ensure_active_registered") as register, \
                mock.patch("bot_bottle.cli.migration_gate.StoreManager") as store, \
                redirect_stderr(err):
            store.return_value.is_migrated.return_value = False
            store.return_value.is_fresh.return_value = True
            code = cli_main.main([command])
        return code, register, store.return_value, err.getvalue()

    def test_sudo_writes_no_per_user_state(self) -> None:
        code, register, store, _ = self._main("fakecmd", sudo=(501, 20), stale=[])
        self.assertEqual(0, code)
        register.assert_not_called()
        store.migrate.assert_not_called()

    def test_a_user_run_registers_and_migrates(self) -> None:
        code, register, store, _ = self._main("fakecmd", sudo=None, stale=[])
        self.assertEqual(0, code)
        register.assert_called_once()
        store.migrate.assert_called_once()

    def test_root_owned_state_stops_with_the_fix(self) -> None:
        stale = [Path("/home/u/.bot-bottle-envs/ci-macos")]
        code, register, _, err = self._main("fakecmd", sudo=None, stale=stale)
        self.assertEqual(1, code)
        register.assert_not_called()
        self.assertIn('sudo chown -R "$(id -un):$(id -gn)" /home/u/.bot-bottle-envs/ci-macos', err)

    def test_doctor_runs_anyway_to_report_it(self) -> None:
        with mock.patch.object(cli_main, "root_owned_state") as check, \
                mock.patch.dict(cli_main.COMMANDS, {"doctor": mock.Mock(return_value=0)}), \
                mock.patch.object(cli_main, "sudo_invoker", return_value=None), \
                mock.patch.object(cli_main, "ensure_active_registered"), \
                mock.patch("bot_bottle.cli.migration_gate.StoreManager"):
            self.assertEqual(0, cli_main.main(["doctor"]))
        check.assert_not_called()


class TestDoctorReportsIt(unittest.TestCase):
    def test_warns_with_the_fix(self) -> None:
        stale = [Path("/home/u/.bot-bottle-envs/ci-macos/env.json")]
        with mock.patch.object(doctor, "root_owned_state", return_value=stale), \
                mock.patch.object(doctor, "_warn") as warn:
            doctor._check_config_dir()  # pylint: disable=protected-access
        label, detail = warn.call_args.args
        self.assertEqual("config", label)
        self.assertIn("owned by root", detail)
        self.assertIn("sudo chown -R", detail)


if __name__ == "__main__":
    unittest.main()
