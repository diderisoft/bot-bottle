"""mitmproxy addon entrypoint for the egress-proxy sidecar (PRD 0017).

Loaded by `mitmdump -s /app/egress_proxy_addon.py` inside the
egress-proxy container. Wraps the pure logic from
`egress_proxy_addon_core` with mitmproxy's HTTPFlow API:

  - At startup, read `EGRESS_PROXY_ROUTES` (default
    `/etc/egress-proxy/routes.yaml`, JSON content) → routes table.
  - SIGHUP re-reads the file and atomically swaps the in-memory
    table. A parse error keeps the old table in place — better to
    keep serving the old config than to leave the proxy with no
    routes after a typo.
  - On each `request`: strip the inbound Authorization header, then
    consult `decide()` for forward / block / inject-auth and apply
    the decision to the flow.

This file imports `mitmproxy` and is never imported on the host —
mitmproxy is a container-only dependency. The host's tests target
`egress_proxy_addon_core`.

Dockerfile.egress-proxy copies both this file and
`egress_proxy_addon_core.py` flat into `/app/`; the absolute import
below works because mitmdump runs with `/app` on its sys.path. The
parallel file in the package source tree (claude_bottle/) is the
build input — not a module the host imports."""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from mitmproxy import http  # type: ignore[import-not-found]

# Absolute import (NOT `from .egress_proxy_addon_core`) — the
# container drops both files flat into /app/ so they are sibling
# top-level modules to mitmdump's loader, not a package.
from egress_proxy_addon_core import Route, decide, load_routes  # type: ignore[import-not-found]


DEFAULT_ROUTES_PATH = "/etc/egress-proxy/routes.yaml"


class EgressProxyAddon:
    """The mitmproxy addon. One instance per `mitmdump` process; the
    request hook is invoked on every CONNECT-decapsulated HTTP/HTTPS
    request the agent makes."""

    def __init__(self) -> None:
        self.routes_path = os.environ.get("EGRESS_PROXY_ROUTES", DEFAULT_ROUTES_PATH)
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
                f"egress-proxy: {tag} load failed: {e}\n"
            )
            if initial:
                # No baseline to fall back on; serve nothing rather
                # than masquerade as a proxy with a route table the
                # operator never declared.
                self.routes = ()
            return
        self.routes = new_routes
        sys.stderr.write(
            f"egress-proxy: loaded {len(self.routes)} route(s): "
            f"{', '.join(r.host for r in self.routes)}\n"
        )

    def _install_sighup(self) -> None:
        if not hasattr(signal, "SIGHUP"):
            return

        def handler(signum: int, frame: object) -> None:
            del signum, frame
            self._reload()

        signal.signal(signal.SIGHUP, handler)

    # mitmproxy's addon API: this method name + signature is how
    # mitmdump discovers the request hook.
    def request(self, flow: http.HTTPFlow) -> None:
        # Inbound Authorization is always stripped — the agent cannot
        # smuggle a stolen token through the proxy. If the matched
        # route declares an auth pair, a fresh header is injected
        # below.
        flow.request.headers.pop("authorization", None)

        request_path = flow.request.path.split("?", 1)[0]
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


addons = [EgressProxyAddon()]
