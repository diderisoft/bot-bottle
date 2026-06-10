"""Unit: smart-HTTP git-gate wrapper."""

import os
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from bot_bottle.git_http_backend import GitHttpHandler, MAX_BODY_BYTES


class TestGitHttpBackend(unittest.TestCase):
    def test_real_git_push_reaches_bare_repo(self):
        from http.server import ThreadingHTTPServer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare = root / "repo.git"
            subprocess.run(["git", "init", "--bare", str(bare)],
                           check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "-C", str(bare), "config", "http.receivepack", "true"],
                check=True,
            )

            old_root = os.environ.get("GIT_PROJECT_ROOT")
            os.environ["GIT_PROJECT_ROOT"] = str(root)
            self.addCleanup(self._restore_env, old_root)
            old_hook = os.environ.get("GIT_GATE_ACCESS_HOOK")
            hook = root / "access-hook"
            hook.write_text("#!/bin/sh\nexit 0\n")
            hook.chmod(0o700)
            os.environ["GIT_GATE_ACCESS_HOOK"] = str(hook)
            self.addCleanup(self._restore_hook, old_hook)

            server = ThreadingHTTPServer(("127.0.0.1", 0), GitHttpHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.shutdown)
            self.addCleanup(server.server_close)

            work = root / "work"
            work.mkdir()
            subprocess.run(["git", "init"], cwd=work, check=True,
                           capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "test"],
                           cwd=work, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"],
                           cwd=work, check=True)
            (work / "README.md").write_text("test\n")
            subprocess.run(["git", "add", "README.md"], cwd=work, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=work,
                           check=True, capture_output=True, text=True)

            url = f"http://127.0.0.1:{server.server_port}/repo.git"
            subprocess.run(
                ["git", "push", url, "HEAD:refs/heads/main"],
                cwd=work,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )

            pushed = subprocess.check_output(
                ["git", "-C", str(bare), "rev-parse", "refs/heads/main"],
                text=True,
            ).strip()
            head = subprocess.check_output(
                ["git", "-C", str(work), "rev-parse", "HEAD"],
                text=True,
            ).strip()
            self.assertEqual(head, pushed)
            subprocess.run(
                ["git", "-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"],
                check=True,
            )

            clone = root / "clone"
            subprocess.run(
                ["git", "clone", url, str(clone)],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            cloned = subprocess.check_output(
                ["git", "-C", str(clone), "rev-parse", "HEAD"],
                text=True,
            ).strip()
            self.assertEqual(head, cloned)

    def test_post_forwards_git_cgi_headers(self):
        from http.server import ThreadingHTTPServer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo.git").mkdir()

            old_root = os.environ.get("GIT_PROJECT_ROOT")
            os.environ["GIT_PROJECT_ROOT"] = str(root)
            self.addCleanup(self._restore_env, old_root)

            server = ThreadingHTTPServer(("127.0.0.1", 0), GitHttpHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.shutdown)
            self.addCleanup(server.server_close)

            backend_response = (
                b"Status: 200 OK\r\n"
                b"Content-Type: application/x-git-upload-pack-result\r\n"
                b"\r\n"
                b"0000"
            )
            calls = [
                subprocess.CompletedProcess(["hook"], 0, b"", b""),
                subprocess.CompletedProcess(["git"], 0, backend_response, b""),
            ]
            with mock.patch(
                "bot_bottle.git_http_backend.subprocess.run",
                side_effect=calls,
            ) as run:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}"
                    "/repo.git/git-upload-pack",
                    data=b"compressed",
                    headers={
                        "Accept": "application/x-git-upload-pack-result",
                        "Content-Encoding": "gzip",
                        "Content-Type": "application/x-git-upload-pack-request",
                        "Git-Protocol": "version=2",
                        "User-Agent": "git/test",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(200, response.status)
                    self.assertEqual(b"0000", response.read())

            env = run.call_args_list[1].kwargs["env"]
            self.assertEqual("gzip", env["HTTP_CONTENT_ENCODING"])
            self.assertEqual("version=2", env["HTTP_GIT_PROTOCOL"])
            self.assertEqual(
                "application/x-git-upload-pack-result",
                env["HTTP_ACCEPT"],
            )
            self.assertEqual("git/test", env["HTTP_USER_AGENT"])

    def test_access_hook_denial_is_logged_to_stdout(self):
        """When the access-hook exits non-zero we still return 403 to the
        client, but the hook's stderr must also appear on the handler's
        stdout so docker logs surface *why* — otherwise the agent sees
        the message and the operator just sees `403 -`."""
        from http.server import ThreadingHTTPServer
        import io
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo.git").mkdir()
            old_root = os.environ.get("GIT_PROJECT_ROOT")
            os.environ["GIT_PROJECT_ROOT"] = str(root)
            self.addCleanup(self._restore_env, old_root)

            server = ThreadingHTTPServer(("127.0.0.1", 0), GitHttpHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.shutdown)
            self.addCleanup(server.server_close)

            denial = b"git-gate: upstream fetch failed; refusing to serve stale data\n"
            with mock.patch(
                "bot_bottle.git_http_backend.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["hook"], 1, b"", denial,
                ),
            ):
                buf = io.StringIO()
                with mock.patch.object(sys, "stdout", buf):
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_port}"
                        "/repo.git/info/refs?service=git-upload-pack",
                        method="GET",
                    )
                    try:
                        urllib.request.urlopen(req, timeout=5)
                        self.fail("expected HTTPError 403")
                    except urllib.error.HTTPError as e:  # type: ignore
                        self.assertEqual(403, e.code)
                        self.assertIn(b"upstream fetch failed", e.read())

                logged = buf.getvalue()
                self.assertIn("access-hook denied", logged)
                self.assertIn("upstream fetch failed", logged)

    def test_access_hook_denial_without_output_logs_exit_code(self):
        """If the hook exits non-zero but produces no stderr/stdout, the
        log line should still say *something* — the exit code — instead
        of silently emitting an empty line."""
        from http.server import ThreadingHTTPServer
        import io
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo.git").mkdir()
            old_root = os.environ.get("GIT_PROJECT_ROOT")
            os.environ["GIT_PROJECT_ROOT"] = str(root)
            self.addCleanup(self._restore_env, old_root)

            server = ThreadingHTTPServer(("127.0.0.1", 0), GitHttpHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.shutdown)
            self.addCleanup(server.server_close)

            with mock.patch(
                "bot_bottle.git_http_backend.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["hook"], 2, b"", b"",
                ),
            ):
                buf = io.StringIO()
                with mock.patch.object(sys, "stdout", buf):
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_port}"
                        "/repo.git/info/refs?service=git-upload-pack",
                        method="GET",
                    )
                    try:
                        urllib.request.urlopen(req, timeout=5)
                        self.fail("expected HTTPError 403")
                    except urllib.error.HTTPError as e:  # type: ignore
                        self.assertEqual(403, e.code)

                logged = buf.getvalue()
                self.assertIn("access-hook denied", logged)
                self.assertIn("exit=2", logged)

    @staticmethod
    def _restore_env(value: str | None) -> None:
        if value is None:
            os.environ.pop("GIT_PROJECT_ROOT", None)
        else:
            os.environ["GIT_PROJECT_ROOT"] = value

    @staticmethod
    def _restore_hook(value: str | None) -> None:
        if value is None:
            os.environ.pop("GIT_GATE_ACCESS_HOOK", None)
        else:
            os.environ["GIT_GATE_ACCESS_HOOK"] = value


class TestContentLengthBounds(unittest.TestCase):
    """PRD 0041: malformed or oversized Content-Length is rejected before
    git http-backend is invoked."""

    def setUp(self):
        from http.server import ThreadingHTTPServer
        import tempfile, os
        self._tmp = tempfile.mkdtemp()
        os.environ["GIT_PROJECT_ROOT"] = self._tmp
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), GitHttpHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
        )
        self._thread.start()
        self._port = self._server.server_port

    def tearDown(self):
        self._server.shutdown()
        self._server.server_close()
        os.environ.pop("GIT_PROJECT_ROOT", None)
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _post(self, path: str, *, content_length_header: str,
              body: bytes = b"x") -> int:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}{path}",
            data=body,
            method="POST",
        )
        req.add_header("Content-Length", content_length_header)
        req.add_header("Content-Type", "application/x-git-receive-pack-request")
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status
        except urllib.error.HTTPError as e:  # type: ignore
            return e.code

    def test_non_numeric_content_length_returns_400(self):
        status = self._post("/repo.git/git-receive-pack",
                            content_length_header="abc")
        self.assertEqual(400, status)

    def test_negative_content_length_returns_400(self):
        status = self._post("/repo.git/git-receive-pack",
                            content_length_header="-1")
        self.assertEqual(400, status)

    def test_oversized_content_length_returns_413(self):
        status = self._post("/repo.git/git-receive-pack",
                            content_length_header=str(MAX_BODY_BYTES + 1))
        self.assertEqual(413, status)

    def test_valid_small_body_passes_through(self):
        # With a valid Content-Length the handler proceeds into
        # git http-backend; that will fail (no real git repo) but the
        # status won't be 400 or 413.
        with mock.patch("bot_bottle.git_http_backend.subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout=(
                    b"Status: 200 OK\r\n"
                    b"Content-Type: application/x-git-receive-pack-result\r\n"
                    b"\r\n"
                ),
            )
            status = self._post("/repo.git/git-receive-pack",
                                content_length_header="1", body=b"x")
        self.assertNotIn(status, (400, 413))


if __name__ == "__main__":
    unittest.main()
