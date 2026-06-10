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

from mitmproxy import http  # type: ignore[import-not-found]  # pylint: disable=import-error

from egress_addon_core import (  # type: ignore[import-not-found]  # pylint: disable=import-error
    LOG_BLOCKS,
    LOG_FULL,
    Config,
    build_inbound_scan_text,
    build_outbound_scan_text,
    decide,
    decide_git_fetch,
    is_git_fetch_request,
    is_git_push_request,
    load_config,
    match_route,
    outbound_scan_headers,
    scan_inbound,
    scan_outbound,
)

try:
    from dlp_detectors import redact_tokens  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - host-side path
    from bot_bottle.dlp_detectors import redact_tokens  # type: ignore[import-not-found]


DEFAULT_ROUTES_PATH = "/etc/egress/routes.yaml"

INTROSPECT_HOST = "_egress.local"


class EgressAddon:
    def __init__(self) -> None:
        self.routes_path = os.environ.get("EGRESS_ROUTES", DEFAULT_ROUTES_PATH)
        self.config: Config = Config(routes=())
        self._reload(initial=True)
        self._install_sighup()

    def _reload(self, *, initial: bool = False) -> None:
        try:
            text = Path(self.routes_path).read_text(encoding="utf-8")
            new_config = load_config(text)
        except (OSError, ValueError) as e:
            tag = "boot" if initial else "SIGHUP"
            sys.stderr.write(
                f"egress: {tag} load failed: {e}\n"
            )
            if initial:
                self.config = Config(routes=())
            return
        self.config = new_config
        log_label = ("off", "blocks", "full")[self.config.log]
        sys.stderr.write(
            f"egress: loaded {len(self.config.routes)} route(s): "
            f"{', '.join(r.host for r in self.config.routes)}"
            f" [log={log_label}]\n"
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
                {"routes": [dataclasses.asdict(r) for r in self.config.routes]},
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

    def _req_ctx(self, flow: http.HTTPFlow) -> dict[str, object]:
        return {
            "host": redact_tokens(flow.request.pretty_host, env=os.environ),
            "method": flow.request.method,
            "path": redact_tokens(flow.request.path, env=os.environ),
        }

    def _block(
        self,
        flow: http.HTTPFlow,
        reason: str,
        ctx: dict[str, object] | None = None,
    ) -> None:
        if self.config.log >= LOG_BLOCKS:
            entry: dict[str, object] = {"event": "egress_block", "reason": reason}
            if ctx:
                entry.update(ctx)
            sys.stderr.write(json.dumps(entry) + "\n")
        flow.response = http.Response.make(
            403,
            reason.encode("utf-8"),
            {"Content-Type": "text/plain; charset=utf-8"},
        )

    def _log_request(self, flow: http.HTTPFlow) -> None:
        sys.stderr.write(
            json.dumps({
                "event": "egress_request",
                "host": redact_tokens(flow.request.pretty_host, env=os.environ),
                "method": flow.request.method,
                "path": redact_tokens(flow.request.path, env=os.environ),
                "headers": dict(flow.request.headers),
                "body": flow.request.get_text(strict=False) or "",
            })
            + "\n"
        )

    def _log_response(self, flow: http.HTTPFlow) -> None:
        sys.stderr.write(
            json.dumps({
                "event": "egress_response",
                "host": flow.request.pretty_host,
                "status": flow.response.status_code,
                "headers": dict(flow.response.headers),
                "body": flow.response.get_text(strict=False) or "",
            })
            + "\n"
        )

    def request(self, flow: http.HTTPFlow) -> None:
        request_path, _, query = flow.request.path.partition("?")

        if flow.request.pretty_host == INTROSPECT_HOST:
            self._serve_introspection(flow, request_path)
            return

        # DLP outbound scan BEFORE stripping auth — catches tokens the
        # agent tried to smuggle in any header, path, query param, or body.
        # Hostname is included to catch DNS-tunnelling exfiltration attempts.
        route = match_route(self.config.routes, flow.request.pretty_host)
        if route is not None:
            body = flow.request.get_text(strict=False) or ""
            scan_text = build_outbound_scan_text(
                flow.request.pretty_host,
                request_path,
                query,
                outbound_scan_headers(route, dict(flow.request.headers)),
                body,
            )
            dlp_result = scan_outbound(route, scan_text, os.environ)
            if dlp_result is not None and dlp_result.severity == "block":
                ctx = self._req_ctx(flow)
                if dlp_result.context:
                    ctx = {**ctx, "context": dlp_result.context}
                self._block(flow, f"egress DLP: {dlp_result.reason}", ctx=ctx)
                return

        if is_git_push_request(request_path, query):
            self._block(
                flow,
                "egress: git push over HTTPS is not supported; "
                "use the bottle.git SSH path (gitleaks-scanned by "
                "git-gate's pre-receive hook).",
                ctx=self._req_ctx(flow),
            )
            return

        if is_git_fetch_request(request_path, query):
            git_decision = decide_git_fetch(
                self.config.routes, flow.request.pretty_host,
            )
            if git_decision.action == "block":
                self._block(
                    flow,
                    git_decision.reason,
                    ctx=self._req_ctx(flow),
                )
                return

        # Strip agent-set Authorization after DLP scan so smuggled tokens
        # are caught above; the route may inject sidecar-owned auth below.
        flow.request.headers.pop("authorization", None)

        # Build headers mapping for match evaluation
        req_headers = {k.lower(): v for k, v in flow.request.headers.items()}

        decision = decide(
            self.config.routes,
            flow.request.pretty_host,
            request_path,
            os.environ,
            request_method=flow.request.method,
            request_headers=req_headers,
        )

        if decision.action == "block":
            self._block(flow, decision.reason, ctx=self._req_ctx(flow))
            return

        if decision.inject_authorization is not None:
            flow.request.headers["authorization"] = decision.inject_authorization

        if self.config.log >= LOG_FULL:
            self._log_request(flow)

    def response(self, flow: http.HTTPFlow) -> None:
        """DLP inbound scan on response headers and body."""
        route = match_route(self.config.routes, flow.request.pretty_host)
        if route is None:
            return
        if flow.response is None:
            return
        if self.config.log >= LOG_FULL:
            self._log_response(flow)
        resp_headers = {k.lower(): v for k, v in flow.response.headers.items()}
        body = flow.response.get_text(strict=False) or ""
        scan_text = build_inbound_scan_text(resp_headers, body)
        if not scan_text:
            return
        result = scan_inbound(route, scan_text)
        if result is None:
            return
        resp_ctx: dict[str, object] = {
            **self._req_ctx(flow),
            "response_status": flow.response.status_code,
        }
        if result.context:
            resp_ctx = {**resp_ctx, "context": result.context}
        if result.severity == "block":
            self._block(flow, f"egress DLP: {result.reason}", ctx=resp_ctx)
        elif result.severity == "warn" and self.config.log >= LOG_BLOCKS:
            sys.stderr.write(
                json.dumps({
                    "event": "egress_warn",
                    "reason": f"egress DLP: {result.reason}",
                    **resp_ctx,
                })
                + "\n"
            )

    def websocket_message(self, flow: http.HTTPFlow) -> None:
        """DLP scan on WebSocket frames.

        Outbound frames (from_client) are scanned for credential leakage;
        inbound frames are scanned for prompt injection.  On a block the
        entire connection is killed — there is no HTTP response surface to
        write to after the upgrade.
        """
        if flow.websocket is None:  # type: ignore[union-attr]
            return
        route = match_route(self.config.routes, flow.request.pretty_host)
        if route is None:
            return
        message = flow.websocket.messages[-1]  # type: ignore[union-attr]
        content = message.content.decode("utf-8", errors="replace")
        if message.from_client:
            result = scan_outbound(route, content, os.environ)
            if result is not None and result.severity == "block":
                sys.stderr.write(f"egress DLP: {result.reason}\n")
                flow.kill()  # type: ignore[union-attr]
        else:
            result = scan_inbound(route, content)
            if result is not None:
                if result.severity == "block":
                    sys.stderr.write(f"egress DLP: {result.reason}\n")
                    flow.kill()  # type: ignore[union-attr]
                elif result.severity == "warn":
                    sys.stderr.write(f"egress DLP warn: {result.reason}\n")


addons = [EgressAddon()]
