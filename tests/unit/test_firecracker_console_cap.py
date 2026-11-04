"""Unit: a microVM's console log is owner-only and capped (issue #934)."""

from __future__ import annotations

import io
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from bot_bottle.backend.firecracker import console_cap, firecracker_vm


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class TestPump(unittest.TestCase):
    def test_writes_an_owner_only_log(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "console.log")
            console_cap.pump(io.BytesIO(b"boot\n"), path)
            self.assertEqual(open(path, "rb").read(), b"boot\n")
            self.assertEqual(_mode(path), 0o600)

    def test_narrows_an_existing_wider_log(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "console.log")
            with open(path, "wb") as f:
                f.write(b"old\n")
            os.chmod(path, 0o644)
            console_cap.pump(io.BytesIO(b"new\n"), path)
            self.assertEqual(_mode(path), 0o600)
            self.assertEqual(open(path, "rb").read(), b"old\nnew\n")

    def test_rotates_at_the_cap_keeping_the_newest_output(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "console.log")

            class _Chunks(io.BufferedIOBase):
                def __init__(self) -> None:
                    self.left = [b"a" * 60, b"b" * 60, b"c" * 60]

                def read1(self, size: int = -1, /) -> bytes:
                    del size
                    return self.left.pop(0) if self.left else b""

            src: Any = _Chunks()
            console_cap.pump(src, path, max_bytes=100)
            self.assertEqual(open(path, "rb").read(), b"c" * 60)
            self.assertEqual(open(f"{path}.1", "rb").read(), b"b" * 60)
            self.assertEqual(_mode(f"{path}.1"), 0o600)

    def test_main_reads_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "console.log")
            stdin = MagicMock()
            stdin.buffer = io.BufferedReader(io.BytesIO(b"hello"))
            with patch("sys.stdin", stdin):
                self.assertEqual(console_cap.main([path, "1000"]), 0)
            self.assertEqual(open(path, "rb").read(), b"hello")

    def test_main_needs_a_path(self) -> None:
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(console_cap.main([]), 2)


class TestBootPipesTheConsole(unittest.TestCase):
    def test_vmm_stdout_goes_through_console_cap(self) -> None:
        cap = MagicMock()
        vmm = MagicMock()
        with tempfile.TemporaryDirectory() as d, \
                patch.object(subprocess, "Popen", side_effect=[cap, vmm]) as popen, \
                patch.object(firecracker_vm, "info"):
            handle = firecracker_vm.boot(
                name="t", kernel=Path("/k"), rootfs=Path("/r"), tap="tap0",
                guest_ip="10.0.0.2", host_ip="10.0.0.1", pubkey="ssh-ed25519 AAAA",
                run_dir=Path(d), detached=True)
        first, second = popen.call_args_list
        self.assertIn("bot_bottle.backend.firecracker.console_cap", first.args[0])
        self.assertEqual(first.kwargs["stdin"], subprocess.PIPE)
        self.assertTrue(first.kwargs["start_new_session"])
        self.assertIs(second.kwargs["stdout"], cap.stdin)
        cap.stdin.close.assert_called_once()
        self.assertIs(handle.process, vmm)


if __name__ == "__main__":
    unittest.main()
