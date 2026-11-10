"""Unit: workspace file read/write for the console (`workspace_file`, `put_workspace_file`)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.backend import adoption
from bot_bottle.backend.base import ExecResult
from bot_bottle.control.hostctl import remote
from bot_bottle.control.hostctl.bottles import BottleService
from bot_bottle.control.hostctl.sessions import SessionRecord
from bot_bottle.control.hostctl.workspace_files import (
    WorkspaceFileError, content_type, read_image, read_script,
    write_file, write_script, _safe_upload_path)
from . import bottled_agent, use_bottle_root

_PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


class LocalBottle:
    """Runs `exec` scripts in a local shell, standing in for the guest."""

    def __init__(self) -> None:
        self.scripts: list[str] = []

    def exec(self, script: str, *, user: str = "node", timeout: float | None = None,
             private: bool = True) -> ExecResult:
        del user, timeout, private
        self.scripts.append(script)
        r = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)
        return ExecResult(r.returncode, r.stdout, r.stderr)


class TestContentType(unittest.TestCase):
    def test_from_the_extension(self) -> None:
        self.assertEqual("image/svg+xml", content_type("/home/node/a/b.SVG"))
        self.assertEqual("image/jpeg", content_type("/tmp/x.jpeg"))
        self.assertEqual("image/png", content_type("/home/node/x.png"))

    def test_refuses_what_isnt_an_image_path(self) -> None:
        for path, why in [("", "file path"), ("/home/node/a\n.png", "file path"),
                          ("/home/node/a\0.png", "file path"),
                          ("home/node/a.png", "absolute"),
                          ("/home/node/../etc/a.png", "normalized"),
                          ("/home/node//a.png", "normalized"),
                          ("/etc/a.png", "only files under"),
                          ("/home/nodex/a.png", "only files under"),
                          ("/home/node", "only files under"),
                          ("/home/node/.ssh/id_ed25519", "only png"),
                          ("/home/node/a.html", "only png"),
                          ("/home/node/" + "a" * 5000 + ".png", "file path")]:
            with self.subTest(path=path[:40]), self.assertRaisesRegex(WorkspaceFileError, why):
                content_type(path)


class TestReadImage(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="bbws."))).resolve()
        self.root = self.dir / "home"
        self.root.mkdir()
        self.roots = (str(self.root),)
        self.bottle = LocalBottle()

    def read(self, name: str, limit: int = 10 * 1024 * 1024) -> dict[str, object]:
        return read_image(self.bottle, str(self.root / name), self.roots, limit)  # type: ignore[arg-type]

    def test_a_png(self) -> None:
        (self.root / "a.png").write_bytes(_PNG)
        got = self.read("a.png")
        self.assertEqual({"content_type": "image/png", "size": len(_PNG),
                          "data": base64.b64encode(_PNG).decode()}, got)

    def test_an_svg_is_sent_as_bytes_typed_by_its_extension(self) -> None:
        (self.root / "icons").mkdir()
        (self.root / "icons" / "c.svg").write_bytes(_SVG)
        got = self.read("icons/c.svg")
        self.assertEqual(("image/svg+xml", _SVG),
                         (got["content_type"], base64.b64decode(str(got["data"]))))

    def test_too_big(self) -> None:
        (self.root / "big.png").write_bytes(b"x" * 101)
        with self.assertRaisesRegex(WorkspaceFileError, "the file is over"):
            self.read("big.png", limit=100)
        (self.root / "fits.png").write_bytes(b"x" * 100)
        self.assertEqual(100, self.read("fits.png", limit=100)["size"])

    def test_missing(self) -> None:
        with self.assertRaisesRegex(WorkspaceFileError, "no such file"):
            self.read("nope.png")
        (self.root / "dir.png").mkdir()
        with self.assertRaisesRegex(WorkspaceFileError, "no such file"):
            self.read("dir.png")

    @unittest.skipIf(os.geteuid() == 0, "root reads anything")
    def test_unreadable(self) -> None:
        (self.root / "locked.png").write_bytes(_PNG)
        (self.root / "locked.png").chmod(0)
        with self.assertRaisesRegex(WorkspaceFileError, "can't read"):
            self.read("locked.png")

    def test_a_symlink_out_of_the_roots_is_refused(self) -> None:
        (self.dir / "secret.png").write_bytes(_PNG)
        (self.root / "escape.png").symlink_to(self.dir / "secret.png")
        with self.assertRaisesRegex(WorkspaceFileError, "links outside"):
            self.read("escape.png")

    def test_a_symlink_to_a_non_image_is_refused(self) -> None:
        (self.root / "notes.txt").write_text("hi")
        (self.root / "pic.png").symlink_to(self.root / "notes.txt")
        with self.assertRaisesRegex(WorkspaceFileError, "isn't an image"):
            self.read("pic.png")

    def test_a_symlink_inside_the_roots_is_followed(self) -> None:
        (self.root / "real.png").write_bytes(_PNG)
        (self.root / "alias.png").symlink_to(self.root / "real.png")
        self.assertEqual(len(_PNG), self.read("alias.png")["size"])

    def test_a_bad_path_never_reaches_the_bottle(self) -> None:
        with self.assertRaisesRegex(WorkspaceFileError, "only png"):
            self.read("a.txt")
        self.assertEqual([], self.bottle.scripts)

    def test_the_path_is_quoted(self) -> None:
        name = "it's $(touch pwned) a.png"
        (self.root / name).write_bytes(_PNG)
        self.assertEqual(len(_PNG), self.read(name)["size"])
        self.assertFalse((Path.cwd() / "pwned").exists())

    def test_a_lying_guest_is_checked_again(self) -> None:
        for stdout, why in [("R /etc/passwd.png\nD AAAA\n", "links outside"),
                            (f"R {self.root}/a.png\n", "couldn't read"),
                            (f"R {self.root}/a.png\nD !!notbase64\n", "couldn't read"),
                            (f"R {self.root}/a.png\nE weird\n", "couldn't read")]:
            bottle = mock.Mock(exec=mock.Mock(return_value=ExecResult(0, stdout, "")))
            with self.subTest(stdout=stdout), self.assertRaisesRegex(WorkspaceFileError, why):
                read_image(bottle, str(self.root / "a.png"), self.roots)
        failed = mock.Mock(exec=mock.Mock(return_value=ExecResult(1, "", "gone")))
        with self.assertRaisesRegex(WorkspaceFileError, "couldn't read"):
            read_image(failed, str(self.root / "a.png"), self.roots)

    def test_the_script_checks_the_resolved_path(self) -> None:
        script = read_script("/home/node/a.png")
        self.assertIn("readlink -f", script)
        self.assertIn("/home/node/*|/tmp/*) ;;", script)


class TestService(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="bbwsvc.")))
        self.addCleanup(use_bottle_root(root))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.sessions = mock.MagicMock()
        self.sessions.record.return_value = SessionRecord(
            id="s1", kind="bottle", meta={"backend": "fake", "activation": "a1"})
        self.sessions.exists.return_value = True
        self.backend = mock.MagicMock()
        self.service = BottleService(self.sessions, backend_for=lambda _s: self.backend,
                                     recorded_infra=lambda _b: None, audit=mock.MagicMock())
        bottled_agent("s1")
        adoption.write("s1", adoption.AdoptionRecord(
            container="c", registry_id="", workspace=adoption.AdoptedWorkspace(
                enabled=True, has_host_git_dir=True, guest_path="/home/node/workspace")))

    def test_reads_in_the_bottle(self) -> None:
        data = base64.b64encode(_PNG).decode()
        bottle = mock.Mock(exec=mock.Mock(return_value=ExecResult(
            0, f"R /home/node/a.png\nD {data}\n", "")))
        self.backend.attach_adopted.return_value = bottle
        got = self.service.read_workspace_image("s1", "/home/node/a.png")
        self.assertEqual(("image/png", len(_PNG)), (got["content_type"], got["size"]))

    def test_errors(self) -> None:
        self.assertEqual({"error": "only png, jpg, gif, webp and svg images can be shown"},
                         self.service.read_workspace_image("s1", "/home/node/x.sh"))
        self.backend.attach_adopted.assert_not_called()
        self.backend.attach_adopted.return_value = None
        self.assertIn("can't reach", str(self.service.read_workspace_image(
            "s1", "/home/node/a.png")["error"]))
        self.sessions.exists.return_value = False
        self.assertIn("isn't running", str(self.service.read_workspace_image(
            "s1", "/home/node/a.png")["error"]))


class TestSafeUploadPath(unittest.TestCase):
    def test_valid_paths(self) -> None:
        for path in ["/home/node/uploads/img.png", "/home/node/uploads/sub/file.txt",
                     "/home/node/uploads/a-b_c.json"]:
            with self.subTest(path=path):
                _safe_upload_path(path)  # should not raise

    def test_refuses_bad_paths(self) -> None:
        for path, why in [
            ("", "file path"), ("/home/node/uploads/a\n.png", "file path"),
            ("/home/node/uploads/a\0.png", "file path"),
            ("home/node/uploads/a.png", "absolute"),
            ("/home/node/uploads/../etc/passwd", "normalized"),
            ("/home/node/a.png", "uploads must go under"),
            ("/tmp/a.png", "uploads must go under"),
            ("/home/nodeevil/uploads/a.png", "uploads must go under"),
        ]:
            with self.subTest(path=path[:50]), self.assertRaisesRegex(WorkspaceFileError, why):
                _safe_upload_path(path)


class TestWriteFile(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="bbww."))).resolve()
        self.upload_root = str(self.dir / "uploads")
        self.bottle = LocalBottle()

    def write(self, name: str, data: bytes) -> dict[str, object]:
        path = f"{self.upload_root}/{name}"
        return write_file(self.bottle, path, data, self.upload_root)  # type: ignore[arg-type]

    def test_writes_a_file(self) -> None:
        data = b"hello world"
        result = self.write("hello.txt", data)
        self.assertEqual({"path": f"{self.upload_root}/hello.txt", "size": 11}, result)
        self.assertEqual(data, Path(f"{self.upload_root}/hello.txt").read_bytes())

    def test_creates_subdirectory(self) -> None:
        data = b"\x89PNG"
        result = self.write("sub/dir/icon.png", data)
        self.assertEqual(data, Path(f"{self.upload_root}/sub/dir/icon.png").read_bytes())
        self.assertEqual(4, result["size"])

    def test_a_png_and_arbitrary_bytes(self) -> None:
        data = _PNG
        result = self.write("img.png", data)
        self.assertEqual(data, Path(f"{self.upload_root}/img.png").read_bytes())
        self.assertEqual(len(_PNG), result["size"])

    def test_too_big_is_refused_before_reaching_the_bottle(self) -> None:
        big = b"x" * (5 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(WorkspaceFileError, "over 5 MiB"):
            write_file(self.bottle, f"{self.upload_root}/big.bin", big, self.upload_root)  # type: ignore[arg-type]
        self.assertEqual([], self.bottle.scripts)

    def test_outside_root_is_refused_before_reaching_the_bottle(self) -> None:
        with self.assertRaisesRegex(WorkspaceFileError, "uploads must go under"):
            write_file(self.bottle, "/home/node/evil.sh", b"bad", self.upload_root)  # type: ignore[arg-type]
        self.assertEqual([], self.bottle.scripts)

    def test_injection_in_path_is_quoted(self) -> None:
        name = "it's $(touch pwned).txt"
        # shlex.quote prevents execution; the file is written with the literal name
        write_file(self.bottle, f"{self.upload_root}/{name}", b"x", self.upload_root)  # type: ignore[arg-type]
        self.assertFalse((Path.cwd() / "pwned").exists())

    def test_exec_failure_raises(self) -> None:
        bottle = mock.Mock(exec=mock.Mock(return_value=ExecResult(1, "", "failed")))
        with self.assertRaisesRegex(WorkspaceFileError, "couldn't write"):
            write_file(bottle, f"{self.upload_root}/x.txt", b"x", self.upload_root)

    def test_guest_outside_reply_raises(self) -> None:
        bottle = mock.Mock(exec=mock.Mock(return_value=ExecResult(0, "E outside\n", "")))
        with self.assertRaisesRegex(WorkspaceFileError, "outside"):
            write_file(bottle, f"{self.upload_root}/x.txt", b"x", self.upload_root)

    def test_the_script_checks_the_path_prefix(self) -> None:
        script = write_script(f"{self.upload_root}/a.txt", "AAAA", self.upload_root)
        self.assertIn("case", script)
        self.assertIn("outside", script)


class TestWriteService(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="bbwwsvc.")))
        self.addCleanup(use_bottle_root(root))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.sessions = mock.MagicMock()
        self.sessions.record.return_value = SessionRecord(
            id="s1", kind="bottle", meta={"backend": "fake", "activation": "a1"})
        self.sessions.exists.return_value = True
        self.backend = mock.MagicMock()
        self.service = BottleService(self.sessions, backend_for=lambda _s: self.backend,
                                     recorded_infra=lambda _b: None, audit=mock.MagicMock())
        bottled_agent("s1")
        adoption.write("s1", adoption.AdoptionRecord(
            container="c", registry_id="", workspace=adoption.AdoptedWorkspace(
                enabled=True, has_host_git_dir=True, guest_path="/home/node/workspace")))

    def test_writes_in_the_bottle(self) -> None:
        bottle = mock.Mock(exec=mock.Mock(return_value=ExecResult(0, "OK\n", "")))
        self.backend.attach_adopted.return_value = bottle
        got = self.service.write_workspace_file("s1", "/home/node/uploads/f.txt", b"hi")
        self.assertEqual({"path": "/home/node/uploads/f.txt", "size": 2}, got)

    def test_bad_path_never_reaches_bottle(self) -> None:
        got = self.service.write_workspace_file("s1", "/home/node/secret.txt", b"x")
        self.assertIn("error", got)
        self.backend.attach_adopted.assert_not_called()

    def test_bottle_unavailable(self) -> None:
        self.backend.attach_adopted.return_value = None
        got = self.service.write_workspace_file("s1", "/home/node/uploads/f.txt", b"x")
        self.assertIn("error", got)

    def test_not_running(self) -> None:
        self.sessions.exists.return_value = False
        got = self.service.write_workspace_file("s1", "/home/node/uploads/f.txt", b"x")
        self.assertIn("error", got)


class TestOnTheLink(unittest.TestCase):
    def setUp(self) -> None:
        self.sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            self.sent.append(message)

        self.bottles = mock.Mock()
        self.bottles.read_workspace_image.return_value = {
            "content_type": "image/png", "data": "AAAA", "size": 3}
        self.bottles.write_workspace_file.return_value = {
            "path": "/home/node/uploads/f.txt", "size": 2}
        self.link = remote.ConsoleLink(self.bottles, send, agents=lambda: [])
        self.link.running["ag1"] = "s1"

    def ask(self, **msg: Any) -> dict[str, Any]:
        asyncio.run(self.link.handle({"type": "workspace_file", "request_id": "q1", **msg}))
        return self.sent[-1]

    def put(self, **msg: Any) -> dict[str, Any]:
        asyncio.run(self.link.handle(
            {"type": "put_workspace_file", "request_id": "q2", **msg}))
        return self.sent[-1]

    def test_a_running_agent(self) -> None:
        self.assertEqual({"type": "workspace_file", "request_id": "q1", "agent_id": "ag1",
                          "path": "/home/node/a.png", "content_type": "image/png",
                          "data": "AAAA", "size": 3},
                         self.ask(agent_id="ag1", path="/home/node/a.png"))
        self.bottles.read_workspace_image.assert_called_once_with("s1", "/home/node/a.png")

    def test_an_unknown_agent(self) -> None:
        self.assertEqual("that agent isn't running on this host",
                         self.ask(agent_id="other", path="/home/node/a.png")["error"])
        self.bottles.read_workspace_image.assert_not_called()

    def test_a_path_that_isnt_text(self) -> None:
        reply = self.ask(agent_id="ag1", path=["/home/node/a.png"])
        self.assertEqual(("", "that isn't a file path"), (reply["path"], reply["error"]))

    def test_an_older_bottle_service(self) -> None:
        del self.bottles.read_workspace_image
        self.assertEqual("this host controller can't read workspace files",
                         self.ask(agent_id="ag1", path="/home/node/a.png")["error"])

    def test_put_a_running_agent(self) -> None:
        import base64 as _b64
        data_b64 = _b64.b64encode(b"hi").decode()
        reply = self.put(agent_id="ag1", path="/home/node/uploads/f.txt", data=data_b64)
        self.assertEqual({"type": "workspace_file_put", "request_id": "q2", "agent_id": "ag1",
                          "path": "/home/node/uploads/f.txt", "size": 2}, reply)
        self.bottles.write_workspace_file.assert_called_once_with(
            "s1", "/home/node/uploads/f.txt", b"hi")

    def test_put_unknown_agent(self) -> None:
        import base64 as _b64
        reply = self.put(agent_id="other", path="/home/node/uploads/f.txt",
                         data=_b64.b64encode(b"x").decode())
        self.assertEqual("that agent isn't running on this host", reply["error"])
        self.bottles.write_workspace_file.assert_not_called()

    def test_put_missing_data(self) -> None:
        reply = self.put(agent_id="ag1", path="/home/node/uploads/f.txt")
        self.assertEqual("the file data is missing", reply["error"])

    def test_put_invalid_base64(self) -> None:
        reply = self.put(agent_id="ag1", path="/home/node/uploads/f.txt", data="!!!notb64")
        self.assertEqual("the file data is not valid base64", reply["error"])

    def test_put_older_bottle_service(self) -> None:
        import base64 as _b64
        del self.bottles.write_workspace_file
        reply = self.put(agent_id="ag1", path="/home/node/uploads/f.txt",
                         data=_b64.b64encode(b"x").decode())
        self.assertEqual("this host controller can't write workspace files", reply["error"])


if __name__ == "__main__":
    unittest.main()
