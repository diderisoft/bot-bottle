"""Unit: the pinned Firecracker release binary, installed when none is on PATH."""

from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from bot_bottle.backend.firecracker import binary


def _tarball(arch: str, body: bytes = b"#!fc") -> bytes:
    member = f"release-{binary.VERSION}-{arch}/firecracker-{binary.VERSION}-{arch}"
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        info = tarfile.TarInfo(member)
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return out.getvalue()


class Ensure(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "bin"
        which = patch.object(binary.shutil, "which", return_value=None)
        which.start()
        self.addCleanup(which.stop)

    def _serve(self, data: bytes) -> tuple[list[str], Callable[[str, Path], None]]:
        urls: list[str] = []

        def download(url: str, dest: Path) -> None:
            urls.append(url)
            dest.write_bytes(data)

        return urls, download

    def test_installs_the_pinned_release_when_none_is_on_path(self) -> None:
        data = _tarball("x86_64")
        urls, download = self._serve(data)
        said: list[str] = []
        with patch.dict(binary.TARBALLS, {"x86_64": "sha256:" + hashlib.sha256(data).hexdigest()}):
            dest = binary.ensure(say=said.append, machine="x86_64", target_dir=self.dir,
                                 download=download)
        self.assertEqual(self.dir / "firecracker", dest)
        self.assertEqual(b"#!fc", (self.dir / "firecracker").read_bytes())
        self.assertEqual(0o755, (self.dir / "firecracker").stat().st_mode & 0o777)
        self.assertEqual([binary.tarball_url("x86_64")], urls)
        self.assertIn(binary.VERSION, said[0])
        # Present now: nothing more to do.
        self.assertIsNone(binary.ensure(machine="x86_64", target_dir=self.dir))

    def test_a_tarball_that_doesnt_match_the_pin_is_refused(self) -> None:
        _urls, download = self._serve(_tarball("x86_64"))
        with patch.dict(binary.TARBALLS, {"x86_64": "sha256:" + "0" * 64}), \
                self.assertRaisesRegex(binary.BinaryError, "not the pinned"):
            binary.ensure(machine="x86_64", target_dir=self.dir,
                          download=download)
        self.assertFalse((self.dir / "firecracker").exists())

    def test_a_tarball_without_the_binary_is_an_error(self) -> None:
        data = _tarball("aarch64")
        _urls, download = self._serve(data)
        with patch.dict(binary.TARBALLS, {"x86_64": "sha256:" + hashlib.sha256(data).hexdigest()}), \
                self.assertRaisesRegex(binary.BinaryError, "has no"):
            binary.ensure(machine="x86_64", target_dir=self.dir,
                          download=download)

    def test_one_on_path_or_an_unknown_arch(self) -> None:
        with patch.object(binary.shutil, "which", return_value="/usr/bin/firecracker"):
            self.assertIsNone(binary.ensure(target_dir=self.dir))
        with self.assertRaisesRegex(binary.BinaryError, "install firecracker yourself"):
            binary.ensure(machine="riscv64", target_dir=self.dir)

    def test_a_failed_download_is_an_error(self) -> None:
        with patch.object(binary.urllib.request, "urlopen", side_effect=OSError("offline")), \
                self.assertRaisesRegex(binary.BinaryError, "couldn't download"):
            binary._download("https://example.invalid/x", self.dir / "x")  # pylint: disable=protected-access


if __name__ == "__main__":
    unittest.main()
