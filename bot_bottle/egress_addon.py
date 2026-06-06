"""mitmproxy addon entrypoint for the egress sidecar (PRD 0017, PRD 0053).

Loaded by `mitmdump -s /app/egress_addon.py` inside the
egress container."""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import sys
from pathlib import Path

from mitmproxy import http  # type: ignore[import-not-found]

from egress_addon_core import (  # type: ignore[import-not-found]
    Route,
    decide,
    is_git_push_request,
    load_routes,
    match_route,
    scan_inbound,
    scan_outbound,
)


DEFAULT_ROUTES_PATH = "/etc/egress/routes.yaml"

INTROSPECT_HOST = "_egress.local"


class EgressAddon:
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

    def _block(self, flow: http.HTTPFlow, reason: str) -> None:
        sys.stderr.write(f"{reason}\n")
        flow.response = http.Response.make(
            403,
            reason.encode("utf-8"),
            {"Content-Type": "text/plain; charset=utf-8"},
        )

    def request(self, flow: http.HTTPFlow) -> None:
        request_path, _, query = flow.request.path.partition("?")

        if flow.request.pretty_host == INTROSPECT_HOST:
            self._serve_introspection(flow, request_path)
            return

        # DLP outbound scan BEFORE stripping auth — catches tokens the
        # agent tried to smuggle in the Authorization header.
        route = match_route(self.routes, flow.request.pretty_host)
        if route is not None:
            body = flow.request.get_text(strict=False) or ""
            auth_header = flow.request.headers.get("authorization", "")
            scan_text = body
            if auth_header:
                scan_text = auth_header + "\n" + body
            dlp_result = scan_outbound(route, scan_text, os.environ)
            if dlp_result is not None and dlp_result.severity == "block":
                self._block(flow, f"egress DLP: {dlp_result.reason}")
                return

        # Strip inbound Authorization — agent cannot smuggle tokens.
        flow.request.headers.pop("authorization", None)

        if is_git_push_request(request_path, query):
            self._block(
                flow,
                "egress: git push over HTTPS is not supported; "
                "use the bottle.git SSH path (gitleaks-scanned by "
                "git-gate's pre-receive hook).",
            )
            return

        # Build headers mapping for match evaluation
        req_headers = {k.lower(): v for k, v in flow.request.headers.items()}

        decision = decide(
            self.routes,
            flow.request.pretty_host,
            request_path,
            os.environ,
            request_method=flow.request.method,
            request_headers=req_headers,
        )

        if decision.action == "block":
            self._block(flow, decision.reason)
            return

        if decision.inject_authorization is not None:
            flow.request.headers["authorization"] = decision.inject_authorization

    def response(self, flow: http.HTTPFlow) -> None:
        """DLP inbound scan on response bodies (PRD 0053)."""
        route = match_route(self.routes, flow.request.pretty_host)
        if route is None:
            return
        if flow.response is None:
            return
        body = flow.response.get_text(strict=False) or ""
        if not body:
            return
        result = scan_inbound(route, body)
        if result is None:
            return
        if result.severity == "block":
            self._block(flow, f"egress DLP: {result.reason}")
        elif result.severity == "warn":
            sys.stderr.write(f"egress DLP warn: {result.reason}\n")


addons = [EgressAddon()]
