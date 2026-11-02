"""Unit: the processes tmux runs for a session (launcher, exit hook), the
local socket's error paths, and the CLI's attach and tmux checks. These run
in-process, without tmux."""

from __future__ import annotations

import io
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.cli.commands import attach as attach_cmd
from bot_bottle.cli.commands import doctor as doctor_cmd
from bot_bottle.control.hostctl import launch, localsock, notify, sessions

from . import use_bottle_root


def _done(returncode: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr="")


class _LocalSocket(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp", prefix="bbe.")))
        self.addCleanup(use_bottle_root(root))
        # The launcher installs signal handlers; keep them out of the test runner.
        for sig in (signal.SIGINT, signal.SIGQUIT, signal.SIGTERM, signal.SIGHUP):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        self.finished: list[tuple[str, sessions.ExitStatus]] = []
        self.manager = sessions.SessionManager(
            {"test": lambda rec, ex: self.finished.append((rec.id, ex))})
        self.sock = sessions.control_socket_path()
        server = localsock.serve(self.sock, self.manager)
        self.addCleanup(server.close)

    def pending(self, session_id: str, argv: list[str]) -> None:
        """Register a session's record and spec as `start` does, with tmux faked:
        `has-session` (none), the config's `source-file` (no server), then
        `new-session`."""
        with mock.patch.object(sessions.subprocess, "run",
                               side_effect=[_done(1), _done(1), _done(0)]):
            self.manager.start(sessions.SessionRecord(id=session_id, kind="test"), argv, {})


class TestLauncher(_LocalSocket):
    def test_runs_the_command_and_reports_its_exit(self) -> None:
        self.pending("e1", ["sh", "-c", "exit 3"])
        self.assertEqual(3, launch.main([str(self.sock), "e1"]))
        self.assertEqual(sessions.ExitStatus(3), self.manager.wait_exit("e1", timeout=5))
        self.assertEqual([("e1", sessions.ExitStatus(3))], self.finished)

    def test_a_spec_is_handed_out_once(self) -> None:
        self.pending("e2", ["true"])
        self.assertEqual(0, launch.main([str(self.sock), "e2"]))
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(127, launch.main([str(self.sock), "e2"]))
        self.assertIn("no pending command", err.getvalue())

    def test_unreachable_daemon_and_missing_command(self) -> None:
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(127, launch.main([str(self.sock) + ".gone", "e3"]))
            self.pending("e4", ["/nonexistent/agent"])
            self.assertEqual(127, launch.main([str(self.sock), "e4"]))
        self.assertIn("can't reach the host controller", err.getvalue())
        self.assertIn("can't start /nonexistent/agent", err.getvalue())


class TestNotifyAndSocket(_LocalSocket):
    def test_notify_reports_an_exit_and_tolerates_a_down_daemon(self) -> None:
        self.pending("n1", ["true"])
        self.assertEqual(0, notify.main([str(self.sock), "n1", "5", "SIGTERM"]))
        self.assertEqual(sessions.ExitStatus(5, "SIGTERM"), self.manager.wait_exit("n1", timeout=5))
        self.assertEqual(1, notify.main([str(self.sock) + ".gone", "n1", "0"]))

    def test_a_second_exit_report_is_ignored(self) -> None:
        self.pending("n2", ["true"])
        notify.main([str(self.sock), "n2", "1"])
        self.manager.wait_exit("n2", timeout=5)
        notify.main([str(self.sock), "n2", "9"])
        self.assertEqual([("n2", sessions.ExitStatus(1))], self.finished)

    def test_rejects_bad_requests_and_unknown_ops(self) -> None:
        self.assertIn("bad request", str(localsock.request(self.sock, {"op": "spec"})["error"]))
        with mock.patch("sys.stderr", io.StringIO()):
            reply = localsock.request(self.sock, {"op": "nope", "session": "x"})
        self.assertIn("unknown op", str(reply["error"]))


class TestAttachCommand(unittest.TestCase):
    def test_usage_missing_tmux_and_missing_session(self) -> None:
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(2, attach_cmd.cmd_attach([]))
            self.assertEqual(0, attach_cmd.cmd_attach(["--help"]))
            with mock.patch.object(attach_cmd.shutil, "which", return_value=None):
                self.assertEqual(1, attach_cmd.cmd_attach(["s1"]))
            with mock.patch.object(attach_cmd.shutil, "which", return_value="/usr/bin/tmux"), \
                 mock.patch.object(attach_cmd.SessionManager, "exists", return_value=False):
                self.assertEqual(1, attach_cmd.cmd_attach(["s1"]))
        self.assertIn("tmux is not installed", err.getvalue())
        self.assertIn("no running session matching 's1'", err.getvalue())

    def test_execs_a_writable_tmux_attach(self) -> None:
        with mock.patch.object(attach_cmd.shutil, "which", return_value="/usr/bin/tmux"), \
             mock.patch.object(attach_cmd.SessionManager, "exists", return_value=True), \
             mock.patch.dict(attach_cmd.os.environ, {"TMUX": "/tmp/user-tmux,1,0"}), \
             mock.patch.object(attach_cmd.os, "execve") as execve:
            attach_cmd.cmd_attach(["s1"])
        argv, env = execve.call_args.args[1:]
        self.assertEqual("/usr/bin/tmux", argv[0])
        self.assertIn("attach-session", argv)
        self.assertNotIn("-r", argv)
        self.assertEqual("=s1", argv[-1])
        # bot-bottle's tmux server is its own: attaching from the user's tmux isn't nesting.
        self.assertNotIn("TMUX", env)


class TestTmuxChecks(unittest.TestCase):
    def test_doctor_requires_tmux_3_1(self) -> None:
        with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            for version, ok in ((None, False), ((3, 0), False), ((3, 4), True)):
                with mock.patch.object(doctor_cmd, "tmux_version", return_value=version):
                    self.assertEqual(ok, doctor_cmd._check_tmux())  # pyright: ignore[reportPrivateUsage]


if __name__ == "__main__":
    unittest.main()
