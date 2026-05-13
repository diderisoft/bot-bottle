"""Cred-proxy HTTP server (PRD 0010).

Runs inside the per-bottle cred-proxy sidecar. Reads
`/run/cred-proxy/routes.json` (laid down by the backend's start step
via `docker cp`) and listens on `0.0.0.0:<port>`. For each request:

  1. Match the request path against the longest route prefix.
  2. Strip any inbound `Authorization` header (the agent cannot
     smuggle a stolen token through this path).
  3. Inject the configured header using the value of the env var
     named by the route's `token_env`.
  4. Forward to the upstream over HTTPS, preserving method, path
     suffix, query string, request body, and the remaining headers.
  5. Stream the response back without buffering — SSE-safe.

The agent talks plain HTTP to this server (loopback-equivalent across
the per-bottle internal docker network). The cred-proxy talks HTTPS
outbound through pipelock to the real upstream. Tokens live in this
container's environ; they never land in routes.json on disk and never
reach the agent's container.

Stdlib-only: this file ships into a minimal Python image with no pip
install layer. The constants are duplicated from `cred_proxy.py` so
the server doesn't need to import the rest of the package.
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import socketserver
import sys
import typing
import urllib.parse
from dataclasses import dataclass


# --- Config / route table ---------------------------------------------------


@dataclass(frozen=True)
class Route:
    """One row of the proxy's route table.

    `path` is the agent-facing prefix (e.g. `/anthropic/`); the
    incoming request's path starts with this. `upstream_scheme` /
    `upstream_host` / `upstream_base_path` are the parsed pieces of
    the upstream URL — the request's path after the prefix is
    appended to `upstream_base_path`. `auth_scheme` is the literal
    word in the injected header (`Bearer` or `token`). `token_env`
    is the env-var name this container reads to get the token."""

    path: str
    upstream_scheme: str
    upstream_host: str
    upstream_port: int
    upstream_base_path: str
    auth_scheme: str
    token_env: str


def parse_routes(payload: dict[str, object]) -> tuple[Route, ...]:
    """Parse the routes.json payload into a tuple of `Route`s. Sorted
    by descending path length so longest-prefix match is the first
    hit in iteration order."""
    raw = payload.get("routes")
    if not isinstance(raw, list):
        raise ValueError("routes.json: 'routes' must be a list")
    out: list[Route] = []
    for r in raw:
        if not isinstance(r, dict):
            raise ValueError(f"routes.json: route must be an object (got {type(r).__name__})")
        path = r["path"]
        upstream = r["upstream"]
        auth_scheme = r["auth_scheme"]
        token_env = r["token_env"]
        if not isinstance(path, str) or not path.startswith("/") or not path.endswith("/"):
            raise ValueError(f"routes.json: path {path!r} must start and end with /")
        if not isinstance(upstream, str):
            raise ValueError("routes.json: upstream must be a string")
        if not isinstance(auth_scheme, str):
            raise ValueError("routes.json: auth_scheme must be a string")
        if not isinstance(token_env, str) or not token_env:
            raise ValueError("routes.json: token_env must be a non-empty string")
        parsed = urllib.parse.urlsplit(upstream)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"routes.json: upstream scheme must be http or https (got {parsed.scheme!r})")
        if not parsed.hostname:
            raise ValueError(f"routes.json: upstream {upstream!r} missing host")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        base_path = parsed.path or ""
        out.append(Route(
            path=path,
            upstream_scheme=parsed.scheme,
            upstream_host=parsed.hostname,
            upstream_port=port,
            upstream_base_path=base_path,
            auth_scheme=auth_scheme,
            token_env=token_env,
        ))
    out.sort(key=lambda r: len(r.path), reverse=True)
    return tuple(out)


def select_route(routes: typing.Sequence[Route], request_path: str) -> Route | None:
    """Return the longest-prefix matching route, or None. Caller is
    responsible for stripping any query string before passing
    `request_path`."""
    for r in routes:
        if request_path.startswith(r.path):
            return r
    return None


# --- Header handling --------------------------------------------------------


# Hop-by-hop headers (RFC 7230 §6.1). Stripped before forwarding.
# Plus `host` (we set it for the upstream) and any `authorization` /
# `proxy-authorization` (the proxy injects its own, never forwards
# the agent's).
_HOP_BY_HOP = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
})

_STRIPPED = _HOP_BY_HOP | frozenset({"host", "authorization", "content-length"})


def build_forward_headers(
    incoming: typing.Iterable[tuple[str, str]],
    *,
    auth_scheme: str,
    token: str,
    upstream_host: str,
) -> list[tuple[str, str]]:
    """Build the header list to send upstream.

    - Strip hop-by-hop headers, the inbound Authorization (the agent
      cannot smuggle a stolen token), and Host (we set it ourselves).
    - Strip Content-Length too: http.client recomputes it when we
      pass `body` to `request()`.
    - Honor the `Connection: close, x, y, z` form by also stripping
      every listed header name.
    - Inject `Authorization: <auth_scheme> <token>` and a Host header
      pointing at the upstream.
    """
    incoming_list = list(incoming)
    # Headers listed in `Connection:` are also hop-by-hop for this hop.
    extra_hop: set[str] = set()
    for name, value in incoming_list:
        if name.lower() == "connection":
            for token_name in value.split(","):
                extra_hop.add(token_name.strip().lower())
    forwarded: list[tuple[str, str]] = []
    for name, value in incoming_list:
        lname = name.lower()
        if lname in _STRIPPED or lname in extra_hop:
            continue
        forwarded.append((name, value))
    forwarded.append(("Host", upstream_host))
    forwarded.append(("Authorization", f"{auth_scheme} {token}"))
    return forwarded


def filter_response_headers(
    incoming: typing.Iterable[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Build the response header list to send back to the agent.
    Strip hop-by-hop + `transfer-encoding` (we let the client's
    HTTP/1.1 default chunking handle streamed bodies)."""
    incoming_list = list(incoming)
    extra_hop: set[str] = set()
    for name, value in incoming_list:
        if name.lower() == "connection":
            for token_name in value.split(","):
                extra_hop.add(token_name.strip().lower())
    out: list[tuple[str, str]] = []
    for name, value in incoming_list:
        lname = name.lower()
        if lname in _HOP_BY_HOP or lname in extra_hop:
            continue
        out.append((name, value))
    return out


# --- HTTP handler -----------------------------------------------------------


# How many bytes to read off the upstream response per chunk. Small
# enough that SSE keep-alive `:` lines (~1 byte) and per-event payloads
# (~hundreds of bytes) round-trip without waiting for a larger buffer
# to fill. Large enough to not dominate syscall overhead under load.
STREAM_CHUNK = 4096


class CredProxyHandler(http.server.BaseHTTPRequestHandler):
    """Per-request handler. The routes + tokens are read off the
    server instance (set by `serve()`)."""

    # Quieter logs: the default writes one line per request to stderr.
    # Useful in debug but noisy in normal operation.
    def log_message(self, format: str, *args: typing.Any) -> None:
        if os.environ.get("CRED_PROXY_DEBUG"):
            super().log_message(format, *args)

    def do_GET(self) -> None: self._proxy()
    def do_POST(self) -> None: self._proxy()
    def do_PUT(self) -> None: self._proxy()
    def do_DELETE(self) -> None: self._proxy()
    def do_PATCH(self) -> None: self._proxy()
    def do_HEAD(self) -> None: self._proxy()
    def do_OPTIONS(self) -> None: self._proxy()

    def _proxy(self) -> None:
        server = typing.cast("CredProxyServer", self.server)
        path, _, query = self.path.partition("?")
        route = select_route(server.routes, path)
        if route is None:
            self.send_error(404, f"no route for {path!r}")
            return
        token = server.tokens.get(route.token_env)
        if not token:
            self.send_error(500, f"cred-proxy: env var {route.token_env} unset in sidecar")
            return

        suffix = path[len(route.path):]
        upstream_path = route.upstream_base_path.rstrip("/") + "/" + suffix
        if query:
            upstream_path = f"{upstream_path}?{query}"

        # Read the request body, if any. We do not stream the body up
        # because http.client doesn't accept a streamable body for
        # arbitrary methods cleanly. v1 buffers — claude's tool-use
        # requests are small JSON payloads; SSE flows are in the
        # response direction only.
        body: bytes | None = None
        length_header = self.headers.get("Content-Length")
        if length_header is not None:
            try:
                length = int(length_header)
            except ValueError:
                self.send_error(400, "invalid Content-Length")
                return
            if length > 0:
                body = self.rfile.read(length)
        elif self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            self.send_error(411, "cred-proxy: chunked request bodies not supported in v1")
            return

        forward_headers = build_forward_headers(
            self.headers.items(),
            auth_scheme=route.auth_scheme,
            token=token,
            upstream_host=route.upstream_host,
        )

        if route.upstream_scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                route.upstream_host, route.upstream_port, timeout=300,
            )
        else:
            conn = http.client.HTTPConnection(
                route.upstream_host, route.upstream_port, timeout=300,
            )

        try:
            conn.request(self.command, upstream_path, body=body,
                         headers=dict(forward_headers))
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            try:
                conn.close()
            except Exception:
                pass
            self.send_error(502, f"upstream connection failed: {e}")
            return

        try:
            self._stream_response(resp)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _stream_response(self, resp: http.client.HTTPResponse) -> None:
        out_headers = filter_response_headers(resp.getheaders())
        # We send Connection: close so the agent's client closes after
        # each request; simplifies streaming bookkeeping and keeps
        # the handler stateless per request.
        self.send_response(resp.status, resp.reason)
        for name, value in out_headers:
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(STREAM_CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Agent disconnected mid-stream; that's fine.
            return


class CredProxyServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threaded HTTP server. `routes` + `tokens` are populated by
    `serve()` before `serve_forever()`."""

    allow_reuse_address = True
    daemon_threads = True

    routes: tuple[Route, ...] = ()
    tokens: dict[str, str] = {}


# --- Entry point ------------------------------------------------------------


DEFAULT_ROUTES_PATH = "/run/cred-proxy/routes.json"
DEFAULT_PORT = 9099


def load_routes(path: str) -> tuple[Route, ...]:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: top-level must be an object")
    return parse_routes(payload)


def load_tokens(routes: tuple[Route, ...], environ: typing.Mapping[str, str]) -> dict[str, str]:
    """Read each route's `token_env` from the supplied environ. Missing
    entries default to empty string; the handler returns 500 for
    unset tokens at request time so the operator can spot the
    misconfig in the cred-proxy's logs without the proxy refusing to
    boot."""
    out: dict[str, str] = {}
    for r in routes:
        out[r.token_env] = environ.get(r.token_env, "")
    return out


def serve(
    *,
    routes_path: str = DEFAULT_ROUTES_PATH,
    port: int = DEFAULT_PORT,
    bind: str = "0.0.0.0",
    environ: typing.Mapping[str, str] | None = None,
) -> typing.NoReturn:
    """Bring up the server and run until killed. Exits non-zero on
    config error so the container's restart policy can surface the
    failure rather than silently retrying."""
    env = environ if environ is not None else os.environ
    routes = load_routes(routes_path)
    tokens = load_tokens(routes, env)
    server = CredProxyServer((bind, port), CredProxyHandler)
    server.routes = routes
    server.tokens = tokens
    sys.stderr.write(
        f"cred-proxy listening on {bind}:{port}; "
        f"{len(routes)} route(s): "
        f"{', '.join(r.path for r in routes)}\n"
    )
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    sys.exit(0)


def main(argv: list[str]) -> int:
    """Tiny argv shim: no flags in v1, all config via env vars.

    `CRED_PROXY_ROUTES` overrides the routes path (default
    `/run/cred-proxy/routes.json`). `CRED_PROXY_PORT` overrides the
    listen port. Both have defaults so the container needs no extra
    config to come up."""
    routes_path = os.environ.get("CRED_PROXY_ROUTES", DEFAULT_ROUTES_PATH)
    port = int(os.environ.get("CRED_PROXY_PORT", str(DEFAULT_PORT)))
    bind = os.environ.get("CRED_PROXY_BIND", "0.0.0.0")
    serve(routes_path=routes_path, port=port, bind=bind)
    return 0  # serve() does not return.


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
