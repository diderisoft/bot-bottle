"""Tiny smart-HTTP wrapper for git-gate repos.

Used where `git://` push traffic over a host-published Docker port can
hang before receive-pack reaches hooks (e.g. the firecracker backend,
where the guest reaches the gateway over the point-to-point TAP). The
wrapper serves the same `/git/*.git` bare repos through
`git http-backend`, so pre-receive and upstream forwarding remain the
git-gate enforcement point.

Consolidated (PRD 0070): when `BOT_BOTTLE_ORCHESTRATOR_URL` is set, one
shared gateway serves every bottle, and each request is served from the
calling bottle's repo namespace (`<root>/<bottle_id>`), attributed from
the unspoofable source IP via the orchestrator. Per-repo credentials +
hooks scope by repo directory, so isolating the *root* per bottle isolates
its creds too. Unattributed clients fail closed (404). Unset → the legacy
per-bottle single-tenant flat root, unchanged — a transitional path that
gets stripped out once every backend runs the consolidated gateway.
"""

from __future__ import annotations

import os
import subprocess
import sys
import typing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

# policy_resolver ships flat alongside this file in the gateway
# image (see Dockerfile.gateway); the bot_bottle.* fallback is the
# host-side / test path. Mirrors egress_addon's import shape.
try:
    from policy_resolver import (  # type: ignore[import-not-found]
        PolicyResolveError,
        PolicyResolver,
    )
except ImportError:  # pragma: no cover - host-side path
    from bot_bottle.policy_resolver import PolicyResolveError, PolicyResolver


DEFAULT_PORT = 9420

# Consolidated (multi-tenant) mode: when this points at the per-host
# orchestrator's control plane, the backend serves each request from the
# *calling* bottle's repo namespace, selected by source IP, instead of a
# single flat repo root. Unset → legacy per-bottle single-tenant mode
# (unchanged). Same env the egress addon reads, so one orchestrator setting
# flips the whole shared gateway multi-tenant.
ORCHESTRATOR_URL_ENV = "BOT_BOTTLE_ORCHESTRATOR_URL"

# App-layer identity token (defense-in-depth over the source-IP invariant);
# the agent injects it, the backend reads it for attribution and never
# forwards it to `git http-backend`. Mirrors egress_addon.IDENTITY_HEADER
# (duplicated, not imported: egress_addon pulls in mitmproxy).
IDENTITY_HEADER = "x-bot-bottle-identity"

# Default flat repo root (single-tenant, and the base under which
# consolidated mode nests each sandbox's namespace).
DEFAULT_REPO_ROOT = "/git"


class ResolverLike(typing.Protocol):
    """Structural type for the resolver `resolve_sandbox_root` needs — just
    `resolve_bottle_id`. A Protocol so tests can pass a fake without
    importing PolicyResolver (mirrors egress_addon_core.PolicyResolverLike)."""

    def resolve_bottle_id(
        self, source_ip: str, identity_token: str = ...,
    ) -> "str | None": ...


def resolve_sandbox_root(
    resolver: "ResolverLike | None",
    base_root: Path,
    source_ip: str,
    identity_token: str = "",
) -> Path | None:
    """The per-sandbox repo root to serve this request from, or None to
    deny (404).

    Single-tenant (`resolver is None`): the flat `base_root`, unchanged.
    NOTE: this legacy per-bottle single-tenant path is transitional — it
    will be stripped out once every backend runs the consolidated gateway
    (PRD 0070), leaving only the source-IP-attributed path below.

    Consolidated: `base_root/<bottle_id>`, where the sandbox is attributed
    from the source IP via the orchestrator. Fail-closed — an unattributed
    client, a resolver error, or a namespace that would escape `base_root`
    all deny, so one sandbox can never reach another's repos."""
    if resolver is None:
        return base_root
    try:
        bottle_id = resolver.resolve_bottle_id(source_ip, identity_token)
    except PolicyResolveError:
        return None  # orchestrator unreachable/errored → deny
    if not bottle_id:
        return None  # unattributed → deny
    base = base_root.resolve()
    namespace = (base / bottle_id).resolve()
    if base not in namespace.parents:
        return None  # bottle_id tried to escape the root → deny
    return namespace

# Mirrors git_gate_render.GIT_GATE_TIMEOUT_SECS. Duplicated rather than
# imported: this module ships as a flat top-level sibling in the gateway
# bundle image (see Dockerfile.gateway), not as part of the bot_bottle
# package, so `bot_bottle.git_gate` and its dependency chain aren't
# available at runtime.
GIT_GATE_TIMEOUT_SECS = 15

# Bound memory use while still allowing ordinary git push packfiles.
MAX_BODY_BYTES = 100 * 1024 * 1024


class GitHttpHandler(BaseHTTPRequestHandler):
    server_version = "bot-bottle-git-http/1"

    def do_GET(self) -> None:
        self._run_backend()

    def do_POST(self) -> None:
        self._run_backend()

    def _sandbox_root(self) -> Path | None:
        """This request's per-sandbox repo root, or None to deny. Single-tenant
        unless the server was started with a resolver (consolidated mode), in
        which case the root is the calling sandbox's source-IP-selected
        namespace. `GIT_PROJECT_ROOT` keeps git's own env-var name."""
        base = Path(os.environ.get("GIT_PROJECT_ROOT", DEFAULT_REPO_ROOT))
        resolver = getattr(self.server, "policy_resolver", None)
        token = self.headers.get(IDENTITY_HEADER, "")
        return resolve_sandbox_root(resolver, base, self.client_address[0], token)

    def _run_backend(self) -> None:
        sandbox_root = self._sandbox_root()
        if sandbox_root is None:
            # Unattributed / resolver error: deny before touching any repo.
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
            hook = subprocess.run(
                [hook_path, "upload-pack", str(repo_dir), peer, peer],
                capture_output=True,
                check=False,
                timeout=GIT_GATE_TIMEOUT_SECS,
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
        # Consolidated mode: attribute the gitleaks-allow supervise proposal
        # (written by receive-pack's pre-receive hook, a child of the CGI we
        # spawn below) to the calling bottle. The namespaced root is
        # `<base>/<bottle_id>`, so its final component is the bottle id — the
        # same per-bottle key egress uses. Single-tenant leaves the hook's
        # container-stamped SUPERVISE_BOTTLE_SLUG untouched.
        if getattr(self.server, "policy_resolver", None) is not None:
            env["SUPERVISE_BOTTLE_SLUG"] = sandbox_root.name
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
            timeout=GIT_GATE_TIMEOUT_SECS,
        )
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
    server = ThreadingHTTPServer(("0.0.0.0", port), GitHttpHandler)
    orch_url = os.environ.get(ORCHESTRATOR_URL_ENV, "").strip()
    # Consolidated mode: resolve each request's sandbox namespace by source
    # IP. Absent → single-tenant (flat repo root); behaviour unchanged.
    resolver = PolicyResolver(orch_url) if orch_url else None
    server.policy_resolver = resolver  # type: ignore[attr-defined]
    mode = "multi-tenant" if orch_url else "single-tenant"
    sys.stdout.write(f"git-http listening on 0.0.0.0:{port} ({mode})\n")
    sys.stdout.flush()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
