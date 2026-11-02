"""Unit: chunked uploads from the console (`upload_begin/chunk/end/abort`). The
scripts run in a local shell against a temp dir standing in for the guest."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from bot_bottle.backend.base import ExecResult
from bot_bottle.control.hostctl import remote_actions, uploads
from bot_bottle.control.hostctl.uploads import UploadError, Uploads


class LocalBottle:
    """Runs `exec` scripts in a local shell (on stdin), standing in for the guest."""

    def __init__(self) -> None:
        self.scripts: list[str] = []

    def exec(self, script: str, *, user: str = "node", timeout: float | None = None,
             private: bool = True) -> ExecResult:
        del user, timeout, private
        self.scripts.append(script)
        r = subprocess.run(["sh", "-s"], input=script, capture_output=True, text=True,
                           check=False)
        return ExecResult(r.returncode, r.stdout, r.stderr)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "uploads")
        self.bottle = LocalBottle()
        self.now = [0.0]
        self.attach_calls: list[str] = []

        def attach(identity: str) -> Any:
            self.attach_calls.append(identity)
            return "bottle gone isn't running" if identity == "gone" else (self.bottle, None, None)

        self.up = Uploads(attach, root=self.root, clock=lambda: self.now[0])

    def send(self, data: bytes, name: str = "clip.mov", *, chunk: int = 5) -> dict[str, object]:
        begun = self.up.begin("a1", "s1", f"{self.root}/{name}", len(data))
        uid = begun["upload_id"]
        for seq, i in enumerate(range(0, len(data), chunk)):
            self.up.chunk("a1", uid, seq, _b64(data[i:i + chunk]))
        return self.up.end("a1", uid, hashlib.sha256(data).hexdigest())


class TestRoundTrip(_Base):
    def test_chunks_land_as_one_file(self) -> None:
        data = os.urandom(23)
        done = self.send(data)
        self.assertEqual({"path": f"{self.root}/clip.mov", "size": 23}, done)
        self.assertEqual(data, Path(self.root, "clip.mov").read_bytes())
        self.assertEqual(["clip.mov"], os.listdir(self.root))  # no partial left

    def test_an_empty_file(self) -> None:
        self.assertEqual(0, self.send(b"")["size"])
        self.assertEqual(b"", Path(self.root, "clip.mov").read_bytes())

    def test_a_taken_name_gets_a_number_never_overwritten(self) -> None:
        first, second, third = os.urandom(4), os.urandom(4), os.urandom(4)
        self.send(first)
        self.assertEqual(f"{self.root}/clip-1.mov", self.send(second)["path"])
        self.assertEqual(f"{self.root}/clip-2.mov", self.send(third)["path"])
        self.assertEqual(first, Path(self.root, "clip.mov").read_bytes())
        self.assertEqual(second, Path(self.root, "clip-1.mov").read_bytes())

    def test_a_name_without_an_extension(self) -> None:
        self.send(b"a", name="notes")
        self.assertEqual(f"{self.root}/notes-1", self.send(b"b", name="notes")["path"])


class TestRefusals(_Base):
    def test_paths_outside_the_upload_root(self) -> None:
        for path in ("/etc/passwd", f"{self.root}/../x", "relative", f"{self.root}/"):
            with self.subTest(path=path):
                with self.assertRaises(UploadError):
                    self.up.begin("a1", "s1", path, 1)
        self.assertEqual([], self.bottle.scripts)  # refused before the bottle

    def test_size_and_shape(self) -> None:
        for size in (-1, "3", True, None, uploads.HOST_UPLOAD_MAX_BYTES + 1):
            with self.subTest(size=size):
                with self.assertRaises(UploadError):
                    self.up.begin("a1", "s1", f"{self.root}/x", size)
        with self.assertRaises(UploadError):
            self.up.begin("a1", "s1", 42, 1)

    def test_a_stopped_bottle(self) -> None:
        with self.assertRaisesRegex(UploadError, "isn't running"):
            self.up.begin("a1", "gone", f"{self.root}/x", 1)

    def test_out_of_order_bad_data_and_overflow(self) -> None:
        uid = self.up.begin("a1", "s1", f"{self.root}/x", 4)["upload_id"]
        with self.assertRaisesRegex(UploadError, "expected chunk 0"):
            self.up.chunk("a1", uid, 1, _b64(b"ab"))
        with self.assertRaisesRegex(UploadError, "base64"):
            self.up.chunk("a1", uid, 0, "not base64!")
        with self.assertRaisesRegex(UploadError, "no data"):
            self.up.chunk("a1", uid, 0, None)
        with self.assertRaisesRegex(UploadError, "more data"):
            self.up.chunk("a1", uid, 0, _b64(b"abcde"))

    def test_another_agent_cant_touch_it(self) -> None:
        uid = self.up.begin("a1", "s1", f"{self.root}/x", 1)["upload_id"]
        for step in (lambda: self.up.chunk("a2", uid, 0, _b64(b"a")),
                     lambda: self.up.end("a2", uid, ""), lambda: self.up.abort("a2", uid)):
            with self.assertRaisesRegex(UploadError, "no such upload"):
                step()

    def test_a_short_or_wrong_upload_is_dropped(self) -> None:
        uid = self.up.begin("a1", "s1", f"{self.root}/x", 4)["upload_id"]
        self.up.chunk("a1", uid, 0, _b64(b"ab"))
        with self.assertRaisesRegex(UploadError, "got 2 of 4"):
            self.up.end("a1", uid, "")
        self.assertEqual([], os.listdir(self.root))  # the partial went
        uid = self.up.begin("a1", "s1", f"{self.root}/x", 2)["upload_id"]
        self.up.chunk("a1", uid, 0, _b64(b"ab"))
        with self.assertRaisesRegex(UploadError, "SHA-256"):
            self.up.end("a1", uid, "0" * 64)
        self.assertEqual([], os.listdir(self.root))

    def test_the_guest_checks_size_and_hash_too(self) -> None:
        uid = self.up.begin("a1", "s1", f"{self.root}/x", 2)["upload_id"]
        self.up.chunk("a1", uid, 0, _b64(b"ab"))
        partial = next(Path(self.root).iterdir())
        partial.write_bytes(b"zz")  # changed under us in the guest
        with self.assertRaisesRegex(UploadError, "doesn't match"):
            self.up.end("a1", uid, hashlib.sha256(b"ab").hexdigest())
        uid = self.up.begin("a1", "s1", f"{self.root}/y", 2)["upload_id"]
        self.up.chunk("a1", uid, 0, _b64(b"ab"))
        next(p for p in Path(self.root).iterdir() if "partial" in p.name).write_bytes(b"abc")
        with self.assertRaisesRegex(UploadError, "isn't the size"):
            self.up.end("a1", uid, hashlib.sha256(b"ab").hexdigest())


class TestAbortAndExpiry(_Base):
    def test_abort_removes_the_partial(self) -> None:
        uid = self.up.begin("a1", "s1", f"{self.root}/x", 4)["upload_id"]
        self.up.chunk("a1", uid, 0, _b64(b"ab"))
        self.assertEqual({"aborted": True}, self.up.abort("a1", uid))
        self.assertEqual([], os.listdir(self.root))
        with self.assertRaises(UploadError):
            self.up.chunk("a1", uid, 1, _b64(b"cd"))

    def test_an_idle_upload_expires(self) -> None:
        uid = self.up.begin("a1", "s1", f"{self.root}/x", 4)["upload_id"]
        self.now[0] = uploads.IDLE_SECONDS + 1
        self.up.begin("a1", "s1", f"{self.root}/y", 1)  # sweeps
        with self.assertRaisesRegex(UploadError, "no such upload"):
            self.up.chunk("a1", uid, 0, _b64(b"ab"))
        self.assertEqual(1, len(os.listdir(self.root)))  # only y's partial

    def test_too_many_at_once(self) -> None:
        for i in range(uploads.MAX_ACTIVE):
            self.up.begin("a1", "s1", f"{self.root}/f{i}", 1)
        with self.assertRaisesRegex(UploadError, "too many"):
            self.up.begin("a1", "s1", f"{self.root}/late", 1)

    def test_a_failing_guest_on_cleanup_is_ignored(self) -> None:
        uid = self.up.begin("a1", "s1", f"{self.root}/x", 1)["upload_id"]
        self.bottle.exec = lambda *a, **k: ExecResult(1, "", "boom")  # type: ignore[method-assign]
        self.assertEqual({"aborted": True}, self.up.abort("a1", uid))


class TestScripts(unittest.TestCase):
    def test_every_script_guards_the_root(self) -> None:
        for script in (uploads.begin_script("/x/p"), uploads.chunk_script("/x/p", "YQ=="),
                       uploads.end_script("/x/p", "/x/q", 1, "a" * 64),
                       uploads.abort_script("/x/p")):
            self.assertIn("/home/node/uploads/*) ;;", script)
            self.assertIn('echo "E outside"', script)

    def test_a_guest_error_is_named(self) -> None:
        class Says:
            def exec(self, *_a: object, **_k: object) -> ExecResult:
                return ExecResult(0, "E mkdir\n", "")
        with self.assertRaisesRegex(UploadError, "upload directory"):
            uploads._run(Says(), "x")  # type: ignore[arg-type]  # pylint: disable=protected-access


class TestLink(unittest.TestCase):
    """The console's messages, through `remote_actions.QUERIES`."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "uploads")
        bottle = LocalBottle()

        class Service:
            def attached(self, _identity: str) -> Any:
                return (bottle, None, None)

        self.service = Service()
        uploads._REGISTRY[id(self.service)] = Uploads(  # pylint: disable=protected-access
            self.service.attached, root=self.root)
        self.addCleanup(uploads._REGISTRY.pop, id(self.service), None)  # pylint: disable=protected-access

    def ask(self, **msg: Any) -> dict[str, Any]:
        handler = remote_actions.QUERIES[msg["type"]]
        return asyncio.run(handler(msg, {"a1": "s1"}, self.service))

    def test_a_whole_upload(self) -> None:
        begun = self.ask(type="upload_begin", request_id="r1", agent_id="a1",
                         path=f"{self.root}/v.mov", size=3)
        self.assertEqual(("upload_begun", "r1"), (begun["type"], begun["request_id"]))
        self.assertEqual(uploads.CHUNK_MAX_BYTES, begun["chunk_bytes"])
        uid = begun["upload_id"]
        ack = self.ask(type="upload_chunk", request_id="r2", agent_id="a1", upload_id=uid,
                       seq=0, data=_b64(b"abc"))
        self.assertEqual(("upload_chunk_ack", 3), (ack["type"], ack["received"]))
        done = self.ask(type="upload_end", request_id="r3", agent_id="a1", upload_id=uid,
                        sha256=hashlib.sha256(b"abc").hexdigest())
        self.assertEqual(("upload_done", f"{self.root}/v.mov", 3),
                         (done["type"], done["path"], done["size"]))

    def test_errors_are_replies(self) -> None:
        r = self.ask(type="upload_begin", request_id="r1", agent_id="nope",
                     path=f"{self.root}/v.mov", size=1)
        self.assertEqual(("upload_begun", "that agent isn't running on this host"),
                         (r["type"], r["error"]))
        r = self.ask(type="upload_abort", request_id="r2", agent_id="a1", upload_id="x")
        self.assertEqual("upload_aborted", r["type"])
        self.assertIn("no such upload", r["error"])

    def test_an_older_service_says_so(self) -> None:
        r = asyncio.run(remote_actions.QUERIES["upload_begin"](
            {"type": "upload_begin", "agent_id": "a1"}, {"a1": "s1"}, object()))
        self.assertIn("can't take chunked uploads", r["error"])


if __name__ == "__main__":
    unittest.main()
