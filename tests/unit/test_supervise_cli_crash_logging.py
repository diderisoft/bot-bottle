"""Unit: supervise launch/crash failure logging (issue #100).

The supervise TUI runs under curses, so anything written to stderr while the
TUI owns the terminal is wiped when the terminal is restored. These
tests lock the recovery paths: a config error (`Die`) is re-surfaced
after the wrapper returns, and an unexpected crash is persisted to a
log file the operator can read.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle import supervise
from bot_bottle.cli import supervise as supervise_cli
from bot_bottle.log import Die, die


class TestDieCarriesMessage(unittest.TestCase):
    def test_die_attaches_message_and_code(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(Die) as cm:
                die("bad manifest: unknown key 'foo'")
        self.assertEqual("bad manifest: unknown key 'foo'", cm.exception.message)
        self.assertEqual(1, cm.exception.code)
        self.assertIn(
            "bot-bottle: error: bad manifest: unknown key 'foo'", buf.getvalue()
        )

    def test_die_default_message_is_empty(self):
        self.assertEqual("", Die(1).message)
        self.assertEqual(1, Die(1).code)


class _FakeHomeMixin:
    """Point supervise.bot_bottle_root (what _write_crash_log resolves
    through) at a temp dir so the crash log doesn't touch the real
    ~/.bot-bottle."""

    def _setup_fake_home(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="supervise-crash-test.")
        self._orig_root = supervise.bot_bottle_root
        self._root = Path(self._tmp.name) / ".bot-bottle"
        supervise.bot_bottle_root = lambda: self._root  # type: ignore[assignment]

    def _teardown_fake_home(self):
        supervise.bot_bottle_root = self._orig_root  # type: ignore[assignment]
        self._tmp.cleanup()


class TestCmdSuperviseErrorPaths(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_keyboard_interrupt_returns_130(self):
        with mock.patch.object(
            supervise_cli.curses, "wrapper", side_effect=KeyboardInterrupt
        ):
            self.assertEqual(130, supervise_cli.cmd_supervise([]))

    def test_die_resurfaces_message_after_curses(self):
        buf = io.StringIO()
        with mock.patch.object(
            supervise_cli.curses, "wrapper",
            side_effect=Die(1, "manifest parse error at line 3"),
        ):
            with contextlib.redirect_stderr(buf):
                rc = supervise_cli.cmd_supervise([])
        self.assertEqual(1, rc)
        self.assertIn("manifest parse error at line 3", buf.getvalue())

    def test_die_without_message_has_fallback(self):
        buf = io.StringIO()
        with mock.patch.object(supervise_cli.curses, "wrapper", side_effect=Die(1)):
            with contextlib.redirect_stderr(buf):
                rc = supervise_cli.cmd_supervise([])
        self.assertEqual(1, rc)
        self.assertIn("fatal error", buf.getvalue())

    def test_unexpected_exception_writes_crash_log(self):
        buf = io.StringIO()
        with mock.patch.object(
            supervise_cli.curses, "wrapper",
            side_effect=ValueError("kaboom in render"),
        ):
            with contextlib.redirect_stderr(buf):
                rc = supervise_cli.cmd_supervise([])
        self.assertEqual(1, rc)
        out = buf.getvalue()
        self.assertIn("supervise crashed: ValueError: kaboom in render", out)
        self.assertIn("full traceback written to", out)
        log_path = self._root / "logs" / "supervise-crash.log"
        self.assertTrue(log_path.exists())
        content = log_path.read_text()
        self.assertIn("kaboom in render", content)
        self.assertIn("Traceback (most recent call last)", content)


class TestWriteCrashLog(_FakeHomeMixin, unittest.TestCase):
    def setUp(self):
        self._setup_fake_home()

    def tearDown(self):
        self._teardown_fake_home()

    def test_appends_traceback_with_header(self):
        try:
            raise RuntimeError("explode")
        except RuntimeError as e:
            path = supervise_cli._write_crash_log(e)
        self.assertEqual(self._root / "logs" / "supervise-crash.log", path)
        text = path.read_text()
        self.assertIn("=== supervise crash", text)
        self.assertIn("RuntimeError: explode", text)

    def test_falls_back_to_tempfile_when_home_unwritable(self):
        # bot_bottle_root points at a *file*, so mkdir under it raises
        # OSError and the helper must fall back to a tempfile.
        bad = Path(self._tmp.name) / "not-a-dir"
        bad.write_text("x")
        supervise.bot_bottle_root = lambda: bad  # type: ignore[assignment]
        try:
            raise RuntimeError("explode2")
        except RuntimeError as e:
            path = supervise_cli._write_crash_log(e)
        self.assertTrue(path.exists())
        self.assertIn("explode2", path.read_text())


if __name__ == "__main__":
    unittest.main()
