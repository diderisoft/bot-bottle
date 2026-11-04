"""The guest rootfs keeps the image's owners and modes (#347)."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.backend.firecracker import image_builder, util
from bot_bottle.backend.firecracker import rootfs_owners as ro


def _tar(*members: tuple[str, str, int, int, int, str]) -> bytes:
    """A tar of (name, type, uid, gid, mode, data-or-linkname) members."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, kind, uid, gid, mode, extra in members:
            info = tarfile.TarInfo(name)
            info.uid, info.gid, info.mode = uid, gid, mode
            data = None
            if kind == "dir":
                info.type = tarfile.DIRTYPE
            elif kind == "link":
                info.type, info.linkname = tarfile.SYMTYPE, extra
            else:
                payload = extra.encode()
                info.size, data = len(payload), io.BytesIO(payload)
            tar.addfile(info, data)
    return buf.getvalue()


_IMAGE = _tar(
    ("./", "dir", 0, 0, 0o755, ""),
    ("./etc/", "dir", 0, 0, 0o755, ""),
    ("./etc/passwd", "file", 0, 0, 0o644, "root:x:0:0\n"),
    ("./usr/", "dir", 0, 0, 0o755, ""),
    ("./usr/bin/", "dir", 0, 0, 0o755, ""),
    ("./usr/bin/su", "file", 0, 0, 0o4755, "#!/bin/sh\n"),
    ("./bin", "link", 0, 0, 0o777, "usr/bin"),
    ("./tmp/", "dir", 0, 0, 0o1777, ""),
    ("./home/", "dir", 0, 0, 0o755, ""),
    ("./home/node/", "dir", 1000, 1000, 0o755, ""),
    ("./home/node/notes", "file", 1000, 1000, 0o600, "mine\n"),
)


class TestRecording(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="bb-owners-")))
        self.tree = self.root / "tree"
        self.tree.mkdir()

    def test_unpack_extracts_and_records_every_member(self) -> None:
        self.assertEqual(0, ro.unpack(io.BytesIO(_IMAGE), self.tree))
        self.assertEqual("mine\n", (self.tree / "home/node/notes").read_text())
        owners = ro.read_owners(self.tree)
        assert owners is not None
        self.assertEqual((0, 0, 0o4755), owners["usr/bin/su"])
        self.assertEqual((1000, 1000, 0o755), owners["home/node"])
        self.assertEqual((0, 0, 0o1777), owners["tmp"])
        self.assertEqual((0, 0, 0o755), owners[""])
        self.assertTrue(ro.has_owners(self.tree))

    def test_a_failed_unpack_records_nothing(self) -> None:
        self.assertNotEqual(0, ro.unpack(io.BytesIO(b"not a tar" * 200), self.tree))
        self.assertFalse(ro.has_owners(self.tree))
        self.assertIsNone(ro.read_owners(self.tree))

    def test_record_from_file_reads_a_compressed_tar(self) -> None:
        path = self.root / "snap.tar.gz"
        with tarfile.open(path, "w:gz") as out, tarfile.open(fileobj=io.BytesIO(_IMAGE)) as src:
            for member in src:
                out.addfile(member, src.extractfile(member) if member.isfile() else None)
        ro.record_from_file(path, self.tree)
        owners = ro.read_owners(self.tree)
        assert owners is not None
        self.assertEqual((0, 0, 0o4755), owners["usr/bin/su"])

    def test_record_from_a_broken_file_fails_closed(self) -> None:
        path = self.root / "broken.tar"
        path.write_bytes(b"\0garbage")
        with self.assertRaises(SystemExit):
            ro.record_from_file(path, self.tree)

    def test_a_later_member_for_a_path_wins(self) -> None:
        data = _tar(("a", "file", 0, 0, 0o644, "x"), ("./a", "file", 5, 6, 0o600, "y"))
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            self.assertEqual({"a": (5, 6, 0o600)}, ro.owners_of(tar))


class TestCommands(unittest.TestCase):
    """Built from a tree whose on-disk owners and modes are known, so the
    result doesn't depend on who runs the tests (CI runs as root, where tar
    keeps owners itself and nothing needs fixing)."""

    def setUp(self) -> None:
        self.tree = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="bb-owners-")))
        (self.tree / "usr/bin").mkdir(parents=True)
        (self.tree / "usr/bin/su").write_text("#!/bin/sh\n")
        os.chmod(self.tree / "usr/bin/su", 0o755)  # as unpacked: setuid lost
        os.symlink("usr/bin", self.tree / "bin")
        self.other = os.getuid() + 7  # an owner the files on disk don't have
        self.owners: ro.Owners = {
            "": (self.other, self.other, 0o755),
            "usr": (self.other, self.other, 0o755),
            "usr/bin": (self.other, self.other, 0o755),
            "usr/bin/su": (self.other, self.other, 0o4755),
            "bin": (self.other, self.other, 0o777),
        }

    def test_each_path_gets_its_recorded_owner_and_lost_mode_bits(self) -> None:
        commands, skipped = ro.ownership_commands(self.tree, self.owners)
        self.assertEqual(0, skipped)
        for path in ("/", "/usr/bin/su", "/bin"):
            self.assertIn(f'sif "{path}" uid {self.other}', commands)
            self.assertIn(f'sif "{path}" gid {self.other}', commands)
        self.assertIn('sif "/usr/bin/su" mode 0104755', commands)
        # A symlink's mode isn't set; one that already matches isn't either.
        self.assertFalse(any(c.startswith('sif "/bin" mode') for c in commands))
        self.assertFalse(any(c.startswith('sif "/usr" mode') for c in commands))

    def test_matching_owners_need_no_commands(self) -> None:
        me = (os.getuid(), os.getgid())
        same = {path: (*me, mode) for path, (_, _, mode) in self.owners.items()}
        os.chmod(self.tree / "usr/bin/su", 0o4755)
        commands, _ = ro.ownership_commands(self.tree, same)
        self.assertEqual([], [c for c in commands if '"/usr/bin/su"' in c])

    def test_a_file_the_tar_did_not_hold_becomes_roots(self) -> None:
        (self.tree / "bb-init").write_text("#!/bin/sh\n")
        commands, _ = ro.ownership_commands(self.tree, self.owners)
        self.assertEqual(os.getuid() != 0, 'sif "/bb-init" uid 0' in commands)
        self.assertFalse(any(c.startswith('sif "/bb-init" mode') for c in commands))

    def test_the_record_itself_is_left_out(self) -> None:
        ro.write_owners(self.tree, self.owners)
        commands, _ = ro.ownership_commands(self.tree, self.owners)
        self.assertFalse(any(ro.OWNERS_FILE in c for c in commands))

    def test_names_debugfs_cannot_take_are_skipped_and_counted(self) -> None:
        (self.tree / 'odd"name').write_text("x")
        commands, skipped = ro.ownership_commands(self.tree, self.owners)
        self.assertEqual(1, skipped)
        self.assertFalse(any("odd" in c for c in commands))


class TestApply(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="bb-owners-")))
        self.tree = self.root / "tree"
        self.tree.mkdir()
        self.image = self.root / "root.ext4"

    def test_a_tree_without_a_record_is_left_alone(self) -> None:
        with patch.object(ro.subprocess, "run") as run:
            ro.apply(self.tree, self.image)
        run.assert_not_called()

    def test_a_debugfs_failure_fails_closed(self) -> None:
        ro.write_owners(self.tree, {"": (0, 0, 0o755)})
        failed = subprocess.CompletedProcess([], 1, "", "boom")
        with patch.object(ro.subprocess, "run", return_value=failed), \
             self.assertRaises(SystemExit):
            ro.apply(self.tree, self.image)

    def test_an_owner_that_did_not_stick_fails_closed(self) -> None:
        ro.write_owners(self.tree, {"": (0, 0, 0o755)})
        ok = subprocess.CompletedProcess([], 0, "User:  1000   Group:   100", "")
        with patch.object(ro.subprocess, "run", return_value=ok), \
             self.assertRaises(SystemExit):
            ro.apply(self.tree, self.image)

    @unittest.skipUnless(shutil.which("mke2fs") and shutil.which("debugfs"), "needs e2fsprogs")
    def test_the_built_image_has_the_images_owners_and_modes(self) -> None:
        self.assertEqual(0, ro.unpack(io.BytesIO(_IMAGE), self.tree))
        (self.tree / "bb-init").write_text("#!/bin/sh\n")
        subprocess.run(["mke2fs", "-q", "-t", "ext4", "-d", str(self.tree), "-F",
                        str(self.image), "16M"], check=True, capture_output=True)
        with patch.object(ro, "warn") as warned:
            ro.apply(self.tree, self.image)
        warned.assert_not_called()

        def stat(path: str) -> str:
            return subprocess.run(["debugfs", "-R", f"stat {path}", str(self.image)],
                                  capture_output=True, text=True, check=False).stdout

        su = stat("/usr/bin/su")
        self.assertIn("Mode:  04755", su)
        self.assertIn("User:     0", su)
        self.assertIn("Group:     0", su)
        self.assertIn("User:  1000", stat("/home/node"))
        self.assertIn("Mode:  0600", stat("/home/node/notes"))
        self.assertIn("Mode:  01777", stat("/tmp"))
        self.assertIn("User:     0", stat("/bb-init"))
        self.assertIn("User:     0", stat("/etc"))
        gone = subprocess.run(["debugfs", "-R", f"stat /{ro.OWNERS_FILE}", str(self.image)],
                              capture_output=True, text=True, check=False)
        self.assertIn("not found", gone.stdout + gone.stderr)


_E2FS = bool(shutil.which("mke2fs") and shutil.which("debugfs"))


def _stat(image: Path, path: str) -> str:
    return subprocess.run(["debugfs", "-R", f"stat {path}", str(image)],
                          capture_output=True, text=True, check=False).stdout


class TestBuildPaths(unittest.TestCase):
    """The three ways a tree is unpacked all record owners, and the ext4 build
    restores them."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="bb-owners-")))
        self.tree = self.root / "tree"
        self.tree.mkdir()
        self.tar = self.root / "image.tar"
        self.tar.write_bytes(_IMAGE)

    def test_an_engine_export_records_owners(self) -> None:
        real_popen = subprocess.Popen

        def popen(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
            if argv[:2] == ["docker", "export"]:
                return real_popen(["cat", str(self.tar)], *args, **kwargs)
            return real_popen(argv, *args, **kwargs)

        created = subprocess.CompletedProcess([], 0, "cid123\n", "")
        with patch.object(util.subprocess, "run", return_value=created), \
             patch.object(util.subprocess, "Popen", side_effect=popen):
            util.export_image_rootfs("docker", "img", self.tree)
        owners = ro.read_owners(self.tree)
        assert owners is not None
        self.assertEqual((0, 0, 0o4755), owners["usr/bin/su"])

    def test_a_failed_engine_export_dies(self) -> None:
        real_popen = subprocess.Popen

        def popen(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
            if argv[:2] == ["docker", "export"]:
                return real_popen(["false"], *args, **kwargs)
            return real_popen(argv, *args, **kwargs)

        created = subprocess.CompletedProcess([], 0, "cid123\n", "")
        with patch.object(util.subprocess, "run", return_value=created), \
             patch.object(util.subprocess, "Popen", side_effect=popen), \
             self.assertRaises(SystemExit):
            util.export_image_rootfs("docker", "img", self.tree)

    def test_a_builder_vm_stream_records_owners(self) -> None:
        argv = ["sh", "-c", f"cat {self.tar}", "ssh"]
        with patch.object(image_builder.util, "ssh_base_argv", return_value=argv):
            image_builder._stream_rootfs(  # pylint: disable=protected-access
                self.root / "key", "10.0.0.2", "tag", "ctr", self.tree)
        owners = ro.read_owners(self.tree)
        assert owners is not None
        self.assertEqual((1000, 1000, 0o755), owners["home/node"])

    @unittest.skipUnless(_E2FS, "needs e2fsprogs")
    def test_build_rootfs_ext4_restores_owners_and_modes(self) -> None:
        self.assertEqual(0, ro.unpack(io.BytesIO(_IMAGE), self.tree))
        image = self.root / "root.ext4"
        util.build_rootfs_ext4(self.tree, image, slack_mib=8)
        self.assertIn("Mode:  04755", _stat(image, "/usr/bin/su"))
        self.assertIn("User:     0", _stat(image, "/usr/bin/su"))
        self.assertIn("User:  1000", _stat(image, "/home/node"))


if __name__ == "__main__":
    unittest.main()
