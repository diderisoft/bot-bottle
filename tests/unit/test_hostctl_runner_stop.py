"""`runner._stop`: SIGTERM, then SIGKILL for a host controller that won't exit."""

from __future__ import annotations

import subprocess
import sys
import unittest
from unittest import mock

from bot_bottle.control.hostctl import runner
from bot_bottle.control.hostctl.runner import HostControlError, HostControlState

# Prints once its SIGTERM handler is in place, then sleeps.
_IGNORES_SIGTERM = (
    "import signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('ready', flush=True)\n"
    "time.sleep(60)\n"
)


class StopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch.object(runner, "STOP_TIMEOUT_SECONDS", 0.5))
        self.enterContext(mock.patch.object(runner, "KILL_TIMEOUT_SECONDS", 0.5))
        self.warn = self.enterContext(mock.patch.object(runner, "warn"))

    def _spawn(self, code: str) -> subprocess.Popen[str]:
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.wait, 5)
        self.addCleanup(proc.kill)
        self.addCleanup(proc.stdout.close)  # type: ignore[union-attr]
        self.assertEqual(proc.stdout.readline().strip(), "ready")  # type: ignore[union-attr]
        return proc

    @staticmethod
    def _state(pid: int) -> HostControlState:
        return HostControlState(pid=pid, port=0, build_id="x")

    def test_one_that_exits_on_sigterm_isnt_killed(self) -> None:
        proc = self._spawn("import time\nprint('ready', flush=True)\ntime.sleep(60)\n")
        runner._stop(self._state(proc.pid))  # pylint: disable=protected-access
        self.warn.assert_not_called()

    def test_one_that_ignores_sigterm_is_killed(self) -> None:
        proc = self._spawn(_IGNORES_SIGTERM)
        runner._stop(self._state(proc.pid))  # pylint: disable=protected-access
        self.warn.assert_called_once()
        self.assertIn("killing it", self.warn.call_args.args[0])

    def test_one_already_gone_is_stopped(self) -> None:
        with mock.patch.object(runner.os, "kill", side_effect=ProcessLookupError):
            runner._stop(self._state(123456))  # pylint: disable=protected-access
        self.warn.assert_not_called()

    def test_one_that_outlives_sigkill_is_an_error(self) -> None:
        with mock.patch.object(runner.os, "kill"), \
                mock.patch.object(runner.os, "waitpid", side_effect=ChildProcessError):
            with self.assertRaisesRegex(HostControlError, "didn't stop"):
                runner._stop(self._state(123456))  # pylint: disable=protected-access

    def test_one_that_dies_between_signals_is_stopped(self) -> None:
        sent: list[int] = []

        def kill(pid: int, signum: int) -> None:
            del pid
            if signum == runner.signal.SIGKILL:
                raise ProcessLookupError
            sent.append(signum)

        with mock.patch.object(runner.os, "kill", side_effect=kill), \
                mock.patch.object(runner.os, "waitpid", side_effect=ChildProcessError):
            runner._stop(self._state(123456))  # pylint: disable=protected-access
        self.assertIn(runner.signal.SIGTERM, sent)


if __name__ == "__main__":
    unittest.main()
