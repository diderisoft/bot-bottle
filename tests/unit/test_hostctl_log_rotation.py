"""Unit: `hostctl.log` rotation, on stand-in descriptors for stdout and
stderr."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from bot_bottle.control.hostctl.log_rotation import LogRotator


class Rotation(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.log = Path(tmp.name) / "hostctl.log"
        # What a launcher does: the log, opened for append, as both streams.
        out = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.fds = (out, os.dup(out))
        for fd in self.fds:
            self.addCleanup(os.close, fd)
        self.rotator = LogRotator(self.log, max_bytes=100, keep=3, fds=self.fds)

    def write(self, text: str, fd: int | None = None) -> None:
        os.write(self.fds[0] if fd is None else fd, text.encode())

    def names(self) -> list[str]:
        return sorted(p.name for p in self.log.parent.iterdir())

    def test_a_small_log_is_left_alone(self) -> None:
        self.write("x" * 99)
        self.assertFalse(self.rotator.check())
        self.assertEqual(["hostctl.log"], self.names())

    def test_a_full_log_is_moved_aside_and_both_streams_follow_the_new_one(self) -> None:
        self.write("old\n" * 30)
        self.assertTrue(self.rotator.check())
        self.write("out\n")
        self.write("err\n", self.fds[1])
        self.assertEqual("out\nerr\n", self.log.read_text())
        self.assertEqual("old\n" * 30, (self.log.parent / "hostctl.log.1").read_text())
        self.assertEqual(0o600, self.log.stat().st_mode & 0o777)

    def test_it_keeps_only_the_newest_files(self) -> None:
        for n in range(5):
            self.write(f"{n}" * 100)
            self.assertTrue(self.rotator.check())
        self.assertEqual(["hostctl.log", "hostctl.log.1", "hostctl.log.2", "hostctl.log.3"],
                         self.names())
        self.assertEqual("4" * 100, (self.log.parent / "hostctl.log.1").read_text())
        self.assertEqual("2" * 100, (self.log.parent / "hostctl.log.3").read_text())

    def test_streams_that_arent_the_log_are_never_touched(self) -> None:
        # Run by hand on a terminal, or the file was moved by someone else.
        self.write("x" * 200)
        os.replace(self.log, self.log.parent / "elsewhere.log")
        self.assertFalse(self.rotator.check())
        self.assertEqual(["elsewhere.log"], self.names())

    def test_the_thread_stops(self) -> None:
        rotator = LogRotator(self.log, max_bytes=100, interval=0.01, fds=self.fds).start()
        rotator.stop()
        rotator._thread.join(1)  # pylint: disable=protected-access
        self.assertFalse(rotator._thread.is_alive())  # pylint: disable=protected-access


if __name__ == "__main__":
    unittest.main()
