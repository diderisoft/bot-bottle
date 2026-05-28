"""mitmproxy addon entrypoint for the egress sidecar (PRD 0017).

Loaded by `mitmdump -s /app/egress_addon.py` inside the
egress container. Wraps the pure logic from
`egress_addon_core` with mitmproxy's HTTPFlow API:

  - At startup, read `EGRESS_ROUTES` (default
    `/etc/egress/routes.yaml`, JSON content) → routes table.
  - SIGHUP re-reads the file and atomically swaps the in-memory
    table. A parse error keeps the old table in place — better to
    keep serving the old config than to leave the proxy with no
    routes after a typo.
  - On each `request`: strip the inbound Authorization header, then
    consult `decide()` for forward / block / inject-auth and apply
    the decision to the flow.

This file imports `mitmproxy` and is never imported on the host —
mitmproxy is a container-only dependency. The host's tests target
`egress_addon_core`.

Dockerfile.sidecars copies both this file and
`egress_addon_core.py` flat into `/app/`; the absolute import
below works because mitmdump runs with `/app` on its sys.path. The
parallel file in the package source tree (bot_bottle/) is the
build input — not a module the host imports."""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import sys
from pathlib import Path

from mitmproxy import http  # type: ignore[import-not-found]

# Absolute import (NOT `from .egress_addon_core`) — the
# container drops both files flat into /app/ so they are sibling
# top-level modules to mitmdump's loader, not a package.
from egress_addon_core import Route, decide, is_git_push_request, load_routes  # type: ignore[import-not-found]


DEFAULT_ROUTES_PATH = "/etc/egress/routes.yaml"

# Magic hostname the addon recognises as an introspection target.
# Requests through the proxy for `_egress.local/<path>` are
# intercepted and answered with synthetic responses (the addon's
# `request` hook sets `flow.response` before any upstream connection).
# The hostname is not in DNS — only clients dialing through this
# specific egress can reach it, and only via HTTP (no TLS).
# Used by the supervise sidecar's `list-egress-routes` MCP
# tool to surface the live route table to the agent.
INTROSPECT_HOST = "_egress.local"


class EgressAddon:
    """The mitmproxy addon. One instance per `mitmdump` process; the
    request hook is invoked on every CONNECT-decapsulated HTTP/HTTPS
    request the agent makes."""

    def __init__(self) -> None:
        self.routes_path = os.environ.get("EGRESS_ROUTES", DEFAULT_ROUTES_PATH)
        self.routes: tuple[Route, ...] = ()
        self._reload(initial=True)
        self._install_sighup()

    def _reload(self, *, initial: bool = False) -> None:
        try:
            text = Path(self.routes_path).read_text(encoding="utf-8")
            new_routes = load_routes(text)
        except (OSError, ValueError) as e:
            tag = "boot" if initial else "SIGHUP"
            sys.stderr.write(
                f"egress: {tag} load failed: {e}\n"
            )
            if initial:
                # No baseline to fall back on; serve nothing rather
                # than masquerade as a proxy with a route table the
                # operator never declared.
                self.routes = ()
            return
        self.routes = new_routes
        sys.stderr.write(
            f"egress: loaded {len(self.routes)} route(s): "
            f"{', '.join(r.host for r in self.routes)}\n"
        )

    def _install_sighup(self) -> None:
        if not hasattr(signal, "SIGHUP"):
            return

        def handler(signum: int, frame: object) -> None:
            del signum, frame
            self._reload()

        signal.signal(signal.SIGHUP, handler)

    def _serve_introspection(self, flow: http.HTTPFlow, path: str) -> None:
        """Synthesize a response for `_egress.local` requests.
        Currently supports `/allowlist` which returns the in-memory
        route table as JSON (host, path_allowlist, auth_scheme,
        token_env per route — no token VALUES, those live in the
        container's environ)."""
        if path == "/allowlist":
            payload = json.dumps(
                {"routes": [dataclasses.asdict(r) for r in self.routes]},
                indent=2,
            ).encode("utf-8")
            flow.response = http.Response.make(
                200, payload,
                {"Content-Type": "application/json"},
            )
            return
        flow.response = http.Response.make(
            404,
            f"egress introspection: no such endpoint {path!r}".encode(),
            {"Content-Type": "text/plain; charset=utf-8"},
        )

    # mitmproxy's addon API: this method name + signature is how
    # mitmdump discovers the request hook.
    def request(self, flow: http.HTTPFlow) -> None:
        request_path, _, query = flow.request.path.partition("?")

        # Introspection: requests to the magic `_egress.local`
        # host are answered locally with a synthetic response. Check
        # before the strip-auth + route logic — these requests aren't
        # real upstream traffic, the agent isn't injecting auth, and
        # the addon's own decide() would 403 the magic host (it's
        # never in the routes table).
        if flow.request.pretty_host == INTROSPECT_HOST:
            self._serve_introspection(flow, request_path)
            return

        # Inbound Authorization is always stripped — the agent cannot
        # smuggle a stolen token through the proxy. If the matched
        # route declares an auth pair, a fresh header is injected
        # below.
        flow.request.headers.pop("authorization", None)

        # Universal HTTPS git-push block. Defense-in-depth: git-gate
        # (PRD 0008) is the only sanctioned outbound path for git
        # writes — its pre-receive runs gitleaks. Letting HTTPS push
        # through egress + auth injection would route around
        # that scan, so we 403 before any route logic.
        if is_git_push_request(request_path, query):
            flow.response = http.Response.make(
                403,
                (
                    b"egress: git push over HTTPS is not supported; "
                    b"use the bottle.git SSH path (gitleaks-scanned by "
                    b"git-gate's pre-receive hook)."
                ),
                {"Content-Type": "text/plain; charset=utf-8"},
            )
            return

        decision = decide(
            self.routes,
            flow.request.pretty_host,
            request_path,
            os.environ,
        )

        if decision.action == "block":
            flow.response = http.Response.make(
                403,
                decision.reason.encode("utf-8"),
                {"Content-Type": "text/plain; charset=utf-8"},
            )
            return

        if decision.inject_authorization is not None:
            flow.request.headers["authorization"] = decision.inject_authorization


addons = [EgressAddon()]
