"""Unit: smart-HTTP git-gate wrapper."""

import os
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from bot_bottle.git_http_backend import GitHttpHandler


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


if __name__ == "__main__":
    unittest.main()
