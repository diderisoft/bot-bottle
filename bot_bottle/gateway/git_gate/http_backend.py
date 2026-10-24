"""git-gate's smart-HTTP server: serves the bare repos through `git
http-backend`, so pre-receive and upstream forwarding stay the enforcement
point.

Each request is served from the calling bottle's namespace
(`<root>/<bottle_id>`), attributed through the orchestrator. Credentials and
hooks live per repo, so separate roots keep each bottle's credentials apart.
Unattributed clients or an unreachable orchestrator get 404.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import typing
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

from bot_bottle.contract.identity import GIT_GATE_TIMEOUT_SECS, GIT_HTTP_PORT, IDENTITY_HEADER
from bot_bottle.gateway.bounded_http import (
    BodyReadError,
    BoundedThreadingHTTPServer,
    copy_declared_body,
)
from bot_bottle.gateway.policy_resolver import PolicyResolveError, PolicyResolver


DEFAULT_PORT = GIT_HTTP_PORT

# The orchestrator each request is attributed through. Mandatory.
ORCHESTRATOR_URL_ENV = "BOT_BOTTLE_ORCHESTRATOR_URL"

# Base under which each `<bottle_id>` namespace is nested.
DEFAULT_REPO_ROOT = "/git"


class ResolverLike(typing.Protocol):
    """What `resolve_sandbox_root` needs from a resolver."""

    def resolve_bottle_id(
        self, source_ip: str, identity_token: str = ...,
    ) -> "str | None": ...


def resolve_sandbox_root(
    resolver: "ResolverLike",
    base_root: Path,
    source_ip: str,
    identity_token: str = "",
) -> Path | None:
    """`base_root/<bottle_id>` for this request, or None (404) if unattributed,
    on a resolver error, or if the id would escape `base_root`."""
    try:
        bottle_id = resolver.resolve_bottle_id(source_ip, identity_token)
    except PolicyResolveError:
        return None  # orchestrator error: deny
    if not bottle_id:
        return None  # unattributed: deny
    base = base_root.resolve()
    namespace = (base / bottle_id).resolve()
    if base not in namespace.parents:
        return None  # escapes the root: deny
    return namespace

# Bounds memory while allowing ordinary push packfiles.
MAX_BODY_BYTES = 100 * 1024 * 1024
REQUEST_BODY_TIMEOUT_SECONDS = 30.0
MAX_REQUEST_WORKERS = 16
MAX_BODY_WORKERS = 2
_BODY_WORK_SLOTS = threading.BoundedSemaphore(MAX_BODY_WORKERS)


class GitHttpHandler(BaseHTTPRequestHandler):
    server_version = "bot-bottle-git-http/1"

    def do_GET(self) -> None:
        self._run_backend()

    def do_POST(self) -> None:
        self._run_backend()

    def _sandbox_root(self) -> Path | None:
        """This request's namespace root, or None to deny. `GIT_PROJECT_ROOT`
        is git's own env name."""
        base = Path(os.environ.get("GIT_PROJECT_ROOT", DEFAULT_REPO_ROOT))
        resolver = getattr(self.server, "policy_resolver", None)
        if resolver is None:
            return None  # no resolver: deny
        token = self.headers.get(IDENTITY_HEADER, "")
        return resolve_sandbox_root(resolver, base, self.client_address[0], token)

    def _run_backend(self) -> None:
        sandbox_root = self._sandbox_root()
        if sandbox_root is None:
            # Deny before touching any repo.
            self.send_error(404)
            return
        parsed = urlsplit(self.path)
        if self._is_upload_pack(parsed.path, parsed.query):
            repo_dir = self._repo_dir(sandbox_root, parsed.path)
            if repo_dir is None:
                self.send_error(404)
                return
            hook_path = os.environ.get(
                "GIT_GATE_ACCESS_HOOK", "/etc/git-gate/access-hook",
            )
            peer = self.client_address[0]
            try:
                hook = subprocess.run(
                    [hook_path, "upload-pack", str(repo_dir), peer, peer],
                    capture_output=True,
                    check=False,
                    timeout=GIT_GATE_TIMEOUT_SECS,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                # Return a real error if the access hook can't run, rather than
                # an empty reply from a dead handler thread.
                self.log_message(
                    "access-hook could not run for %s: %s", parsed.path, exc,
                )
                self.send_error(503, "git-gate access-hook unavailable")
                return
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
            "GIT_PROJECT_ROOT": str(sandbox_root),
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
        # Lets the pre-receive hook attribute any gitleaks-allow proposal to
        # the same (source_ip, identity_token) that picked the namespace.
        env["SUPERVISE_SOURCE_IP"] = self.client_address[0]
        env["SUPERVISE_IDENTITY_TOKEN"] = self.headers.get(IDENTITY_HEADER, "")
        # The namespace is the bottle id; the hook's audit events name it.
        env["GIT_GATE_BOTTLE"] = sandbox_root.name
        for header, variable in (
            ("accept", "HTTP_ACCEPT"),
            ("content-encoding", "HTTP_CONTENT_ENCODING"),
            ("git-protocol", "HTTP_GIT_PROTOCOL"),
            ("user-agent", "HTTP_USER_AGENT"),
        ):
            value = self.headers.get(header)
            if value:
                env[variable] = value
        if not _BODY_WORK_SLOTS.acquire(blocking=False):
            self.send_error(503, "git request capacity exhausted")
            return
        try:
            with tempfile.TemporaryFile() as body:
                try:
                    copy_declared_body(
                        self.rfile,
                        body,
                        self.connection,
                        self.headers.get("content-length"),
                        maximum=MAX_BODY_BYTES,
                        timeout_seconds=REQUEST_BODY_TIMEOUT_SECONDS,
                        require_length=False,
                    )
                except BodyReadError as exc:
                    self.send_error(exc.status, exc.message)
                    return
                body.seek(0)
                try:
                    proc = subprocess.run(
                        ["git", "http-backend"],
                        stdin=body,
                        env=env,
                        capture_output=True,
                        check=False,
                        timeout=GIT_GATE_TIMEOUT_SECS,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    self.log_message("git http-backend unavailable: %s", exc)
                    self.send_error(503, "git backend unavailable")
                    return
        finally:
            _BODY_WORK_SLOTS.release()
        self._write_cgi_response(proc.stdout)

    def _repo_dir(self, sandbox_root: Path, path: str) -> Path | None:
        root = sandbox_root.resolve()
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
    orch_url = os.environ.get(ORCHESTRATOR_URL_ENV, "").strip()
    if not orch_url:
        # Without an orchestrator nothing can be attributed: don't serve.
        sys.stderr.write(
            f"git-http: {ORCHESTRATOR_URL_ENV} is required "
            "(no single-tenant flat-root fallback)\n"
        )
        return 1
    server = BoundedThreadingHTTPServer(
        ("0.0.0.0", port), GitHttpHandler, max_workers=MAX_REQUEST_WORKERS,
    )
    # Attribute each request through the orchestrator.
    server.policy_resolver = PolicyResolver(orch_url)  # type: ignore[attr-defined]
    sys.stdout.write(f"git-http listening on 0.0.0.0:{port} (multi-tenant)\n")
    sys.stdout.flush()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
