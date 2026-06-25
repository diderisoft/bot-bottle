"""Tiny smart-HTTP wrapper for git-gate repos.

Used by the smolmachines backend where `git://` push traffic over the
host-published Docker port can hang before receive-pack reaches hooks.
The wrapper serves the same `/git/*.git` bare repos through
`git http-backend`, so pre-receive and upstream forwarding remain the
git-gate enforcement point.
"""

from __future__ import annotations

import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .git_gate import GIT_GATE_DAEMON_TIMEOUT_SECS


DEFAULT_PORT = 9420

# Bound memory use while still allowing ordinary git push packfiles.
MAX_BODY_BYTES = 100 * 1024 * 1024


class GitHttpHandler(BaseHTTPRequestHandler):
    server_version = "bot-bottle-git-http/1"

    def do_GET(self) -> None:
        self._run_backend()

    def do_POST(self) -> None:
        self._run_backend()

    def _run_backend(self) -> None:
        parsed = urlsplit(self.path)
        if self._is_upload_pack(parsed.path, parsed.query):
            repo_dir = self._repo_dir(parsed.path)
            if repo_dir is None:
                self.send_error(404)
                return
            hook_path = os.environ.get(
                "GIT_GATE_ACCESS_HOOK", "/etc/git-gate/access-hook",
            )
            peer = self.client_address[0]
            hook = subprocess.run(
                [hook_path, "upload-pack", str(repo_dir), peer, peer],
                capture_output=True,
                check=False,
                timeout=GIT_GATE_DAEMON_TIMEOUT_SECS,
            )
            if hook.returncode != 0:
                detail = (hook.stderr or hook.stdout).decode(
                    "utf-8", errors="replace",
                ).rstrip()
                if detail:
                    for line in detail.splitlines():
                        self.log_message("access-hook denied %s: %s",
                                         parsed.path, line)
                else:
                    self.log_message(
                        "access-hook denied %s: exit=%d (no output)",
                        parsed.path, hook.returncode,
                    )
                self.send_response(403)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(hook.stderr or hook.stdout)
                return
        env = os.environ.copy()
        env.update({
            "GIT_PROJECT_ROOT": os.environ.get("GIT_PROJECT_ROOT", "/git"),
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": self.command,
            "PATH_INFO": parsed.path,
            "QUERY_STRING": parsed.query,
            "CONTENT_TYPE": self.headers.get("content-type", ""),
            "CONTENT_LENGTH": self.headers.get("content-length", "0"),
            "REMOTE_ADDR": self.client_address[0],
            "REMOTE_PORT": str(self.client_address[1]),
            "REMOTE_USER": "",
            "SERVER_NAME": self.server.server_name,  # type: ignore
            "SERVER_PORT": str(self.server.server_port),  # type: ignore
            "SERVER_PROTOCOL": self.request_version,
        })
        for header, variable in (
            ("accept", "HTTP_ACCEPT"),
            ("content-encoding", "HTTP_CONTENT_ENCODING"),
            ("git-protocol", "HTTP_GIT_PROTOCOL"),
            ("user-agent", "HTTP_USER_AGENT"),
        ):
            value = self.headers.get(header)
            if value:
                env[variable] = value
        raw_length = self.headers.get("content-length", "0") or "0"
        try:
            length = int(raw_length)
        except ValueError:
            self.send_error(400, "Bad Content-Length")
            return
        if length < 0:
            self.send_error(400, "Negative Content-Length")
            return
        if length > MAX_BODY_BYTES:
            self.send_error(413, "Request body too large")
            return
        body = self.rfile.read(length) if length else b""
        proc = subprocess.run(
            ["git", "http-backend"],
            input=body,
            env=env,
            capture_output=True,
            check=False,
            timeout=GIT_GATE_DAEMON_TIMEOUT_SECS,
        )
        self._write_cgi_response(proc.stdout)

    def _repo_dir(self, path: str) -> Path | None:
        root = Path(os.environ.get("GIT_PROJECT_ROOT", "/git")).resolve()
        relative = path.lstrip("/").split(".git", 1)[0] + ".git"
        candidate = (root / relative).resolve()
        if root not in (candidate, *candidate.parents):
            return None
        if not candidate.is_dir():
            return None
        return candidate

    @staticmethod
    def _is_upload_pack(path: str, query: str) -> bool:
        if path.endswith("/git-upload-pack"):
            return True
        if path.endswith("/info/refs"):
            return any(
                pair == "service=git-upload-pack"
                for pair in query.split("&")
            )
        return False

    def _write_cgi_response(self, raw: bytes) -> None:
        head, sep, body = raw.partition(b"\r\n\r\n")
        line_sep = b"\r\n"
        if not sep:
            head, sep, body = raw.partition(b"\n\n")
            line_sep = b"\n"
        status = 200
        headers: list[tuple[str, str]] = []
        for line in head.split(line_sep):
            if not line:
                continue
            key, _, value = line.decode("latin1").partition(":")
            value = value.strip()
            if key.lower() == "status":
                try:
                    status = int(value.split()[0])
                except (ValueError, IndexError):
                    self.log_message(
                        "malformed CGI Status header %r; using 500", value,
                    )
                    status = 500
            else:
                headers.append((key, value))
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # type: ignore  # noqa: A002
        sys.stdout.write(format % args + "\n")
        sys.stdout.flush()


def main() -> int:
    port = int(os.environ.get("GIT_HTTP_PORT", str(DEFAULT_PORT)))
    server = ThreadingHTTPServer(("0.0.0.0", port), GitHttpHandler)
    sys.stdout.write(f"git-http listening on 0.0.0.0:{port}\n")
    sys.stdout.flush()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
