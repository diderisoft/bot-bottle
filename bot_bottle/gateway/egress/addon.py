"""mitmproxy addon entrypoint for the egress gateway (PRD 0017, PRD 0053).

Loaded by `mitmdump -s /app/egress_addon.py` inside the
egress container."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import sys
import typing

from mitmproxy import http  # type: ignore[import-not-found]  # pylint: disable=import-error

from bot_bottle.constants import IDENTITY_HEADER
from bot_bottle.gateway.egress.dlp_detectors import redact_tokens, strip_crlf
from bot_bottle.gateway.egress.dlp_config import (
    DEFAULT_OUTBOUND_ON_MATCH,
    ON_MATCH_BLOCK,
    ON_MATCH_REDACT,
)
from bot_bottle.gateway.egress.context import resolve_client_context
from bot_bottle.gateway.egress.dlp import (
    build_inbound_scan_text,
    build_outbound_scan_text,
    build_token_allow_payload,
    outbound_scan_headers,
    scan_inbound,
    scan_outbound,
)
from bot_bottle.gateway.egress.matching import (
    decide,
    match_route,
)
from bot_bottle.gateway.egress.request_pipeline import (
    evaluate_route_policy,
    git_block_reason,
)
from bot_bottle.gateway.egress.schema import route_to_yaml_dict
from bot_bottle.gateway.egress.types import (
    LOG_BLOCKS,
    LOG_FULL,
    Config,
    Route,
    ScanResult,
)
from bot_bottle.gateway.policy_resolver import PolicyResolveError, PolicyResolver
from bot_bottle.supervisor.types import (
    STATUS_APPROVED,
    STATUS_MODIFIED,
    STATUSES,
    TOOL_EGRESS_TOKEN_ALLOW,
)


INTROSPECT_HOST = "_egress.local"

# The per-host orchestrator control plane the addon resolves every request's
# Config against, by source IP (PRD 0070). Mandatory: the consolidated gateway
# is the only topology now — there is no static per-bottle routes file to fall
# back to — so an unset value is a fatal misconfiguration (see __init__).
ORCHESTRATOR_URL_ENV = "BOT_BOTTLE_ORCHESTRATOR_URL"

# Per-flow key under which `request()` stashes the resolved (Config, supervise
# slug, env) so the later `response()` and `websocket_message()` hooks scan
# against the *calling bottle's* policy — the same one the request was decided
# on — without a second `/resolve` per response or per WebSocket frame. A hook
# on a flow that never resolved (no stash) fails closed to deny-all, so it's a
# safe no-op rather than an unscanned pass.
_FLOW_CTX_KEY = "bot_bottle_egress_ctx"


def _token_from_proxy_auth(header: str) -> str:
    """Extract the identity token (the password) from a `Proxy-Authorization:
    Basic base64(<bottle_id>:<token>)` header. Empty on any malformed value —
    the mandatory `/resolve` then fail-closes on the empty token."""
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return ""
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return ""
    _, _, password = decoded.partition(":")
    return password

# Seconds the egress proxy holds a token-blocked request open waiting for the
# operator's supervisor decision (PRD 0062), overridable via env.
DEFAULT_TOKEN_ALLOW_TIMEOUT_SECONDS = 300.0

# Maximum bytes of a response body passed to the DLP inbound scan. mitmproxy
# buffers the full response before the hook fires; capping at scan time limits
# the additional memory amplification from decoded text and regex match strings.
# A cap is a security trade-off (content above the threshold is not scanned),
# but without it a single large download OOM-kills the shared egress process
# (issue #455). Override with EGRESS_INBOUND_SCAN_LIMIT_BYTES; set to 0 to
# disable the cap.
DEFAULT_INBOUND_SCAN_LIMIT_BYTES = 1 * 1024 * 1024  # 1 MiB
# Filesystem poll cadence while awaiting the operator's response.
TOKEN_ALLOW_POLL_INTERVAL_SECONDS = 0.5

# Fixed operator guidance attached to every token-allow proposal.
_TOKEN_ALLOW_JUSTIFICATION = (
    "egress DLP blocked an outbound request carrying a detected token. "
    "Approve only if this value is a false positive or a credential this "
    "request legitimately needs; the value is then allowed for the life of "
    "this bottle's egress proxy."
)


class EgressAddon:
    # Bare annotations (no class value): __init__ sets a live PolicyResolver for
    # real runs, and every host-side test builds an addon via __new__ and sets a
    # fake resolver. Egress is resolver-only now — the per-request policy always
    # comes from the orchestrator's /resolve (PRD 0070); there is no static
    # per-bottle routes file, SIGHUP reload, or single-tenant fallback.
    _resolver: "PolicyResolver"
    # Class defaults so __new__-built addons have them (real runs get fresh
    # per-instance collections in __init__; only http_connect mutates them,
    # which request-flow tests don't exercise unless they call http_connect).
    _conn_tokens: "dict[str, str]" = {}
    _passthrough_conns: "set[str]" = set()
    _inbound_scan_limit: int = DEFAULT_INBOUND_SCAN_LIMIT_BYTES

    def __init__(self) -> None:
        # Resolver-only: the gateway is always multi-tenant, resolving each
        # request's policy by source IP against the orchestrator control plane
        # (PRD 0070). The URL is mandatory — without a policy source the gateway
        # must not come up (fail-closed), rather than silently allowing nothing.
        orch_url = os.environ.get(ORCHESTRATOR_URL_ENV, "").strip()
        if not orch_url:
            raise RuntimeError(
                f"{ORCHESTRATOR_URL_ENV} is required: the egress gateway "
                "resolves every request's policy from the orchestrator and has "
                "no static routes file to fall back to."
            )
        self._resolver = PolicyResolver(orch_url)
        # Tokens the operator has approved this session (PRD 0062), keyed by
        # bottle so the shared gateway keeps each bottle's safelist separate —
        # a global set would let bottle A's approved secret pass bottle B's DLP
        # scan. In-memory only (a restart re-prompts); mutated only from the
        # asyncio loop that runs the addon hooks, so no lock is needed.
        self._safe_tokens: dict[str, set[str]] = {}
        # Per-client-connection identity token captured from the CONNECT's
        # `Proxy-Authorization` (HTTPS tunnels don't repeat it on the bumped
        # inner requests). Keyed by client_conn.id; cleared on disconnect.
        self._conn_tokens: dict[str, str] = {}
        # Connections whose route carries `inspect: false` — mitmproxy tunnels
        # these without TLS interception so the client sees the server's real
        # cert. Keyed by client_conn.id; cleared on disconnect.
        self._passthrough_conns: set[str] = set()
        self._token_allow_timeout = _token_allow_timeout_from_env(os.environ)
        self._inbound_scan_limit = _inbound_scan_limit_from_env(os.environ)

    @staticmethod
    def _supervise_available(slug: str) -> bool:
        """Supervise is reachable for this request iff we resolved a bottle to
        attribute its proposals to (the source-IP-attributed bottle id). Empty
        → fail closed (no queue to write to)."""
        return bool(slug)

    def _safe_tokens_for(self, slug: str) -> set[str]:
        """This bottle's operator-approved DLP safelist (PRD 0062), created on
        first use. Keyed by bottle so the shared gateway never leaks one
        bottle's approved token into another's scan."""
        return self._safe_tokens.setdefault(slug, set())

    def _serve_introspection(
        self, flow: http.HTTPFlow, path: str, config: Config,
    ) -> None:
        """Serve the calling bottle's own allowlist. `config` is this flow's
        resolved policy (the same one every hook uses), so the agent sees the
        routes that actually apply to it."""
        if path == "/allowlist":
            payload = json.dumps(
                {"routes": [route_to_yaml_dict(r) for r in config.routes]},
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

    def _flow_log(self, flow: http.HTTPFlow) -> int:
        """This flow's log level, from the policy `request()` resolved and
        stashed. The block/redact log gates were a single global in the static-
        config world; they are per bottle now, so they read it from the flow."""
        return self._flow_ctx(flow)[0].log

    def _req_ctx(self, flow: http.HTTPFlow) -> dict[str, object]:
        # Redact with this flow's resolved env overlay (process env + the
        # bottle's /resolve tokens), so the ctx scrubs the calling bottle's
        # provisioned secrets, not just os.environ's.
        env = self._flow_ctx(flow)[2]
        return {
            "host": redact_tokens(flow.request.pretty_host, env=env),
            "method": flow.request.method,
            "path": redact_tokens(flow.request.path, env=env),
        }

    def _block(
        self,
        flow: http.HTTPFlow,
        reason: str,
        ctx: dict[str, object] | None = None,
    ) -> None:
        if self._flow_log(flow) >= LOG_BLOCKS:
            entry: dict[str, object] = {"event": "egress_block", "reason": reason}
            if ctx:
                entry.update(ctx)
            sys.stderr.write(json.dumps(entry) + "\n")
        flow.response = http.Response.make(
            403,
            reason.encode("utf-8"),
            {"Content-Type": "text/plain; charset=utf-8"},
        )

    def _log_request(
        self, flow: http.HTTPFlow, env: "typing.Mapping[str, str]",
    ) -> None:
        # `env` is the per-flow resolved overlay (process env + this bottle's
        # /resolve tokens), so the log redaction scrubs the calling bottle's
        # provisioned secrets — not just the process-level ones in os.environ.
        headers = {
            k: redact_tokens(v, env=env)
            for k, v in flow.request.headers.items()
            if k.lower() != "authorization"
        }
        body = redact_tokens(flow.request.get_text(strict=False) or "", env=env)
        sys.stderr.write(
            json.dumps({
                "event": "egress_request",
                "host": redact_tokens(flow.request.pretty_host, env=env),
                "method": flow.request.method,
                "path": redact_tokens(flow.request.path, env=env),
                "headers": headers,
                "body": body,
            })
            + "\n"
        )

    def _log_response(
        self, flow: http.HTTPFlow, env: "typing.Mapping[str, str]",
    ) -> None:
        # Per-flow env overlay (see _log_request): redact this bottle's tokens.
        headers = {
            k: redact_tokens(v, env=env)
            for k, v in flow.response.headers.items()
        }
        body = redact_tokens(flow.response.get_text(strict=False) or "", env=env)
        sys.stderr.write(
            json.dumps({
                "event": "egress_response",
                "host": flow.request.pretty_host,
                "status": flow.response.status_code,
                "headers": headers,
                "body": body,
            })
            + "\n"
        )

    def _resolve_flow(
        self, flow: http.HTTPFlow,
    ) -> "tuple[Config, str, typing.Mapping[str, str]]":
        """The calling bottle's `(Config, supervise slug, env)`, resolved by
        source IP in one round-trip against the orchestrator — fail-closed to
        deny-all + empty slug if unattributed. `env` is the process env overlaid
        with the bottle's `/resolve` tokens, so upstream-auth injection (and DLP)
        use *this* bottle's credentials. The identity token, if the agent
        injected one, is read then stripped so it never leaks upstream."""
        conn = flow.client_conn
        client_ip = conn.peername[0] if conn and conn.peername else ""
        token = self._request_token(flow)
        config, slug, tokens = resolve_client_context(self._resolver, client_ip, token)
        env = {**os.environ, **tokens} if tokens else os.environ
        return config, slug, env

    def _stash_flow_ctx(
        self,
        flow: http.HTTPFlow,
        config: Config,
        slug: str,
        env: "typing.Mapping[str, str]",
    ) -> None:
        """Remember the per-flow context `request()` resolved, so the later
        `response()` / `websocket_message()` hooks reuse it — scanning against
        the same bottle's policy the request was decided on, with one `/resolve`
        per flow rather than one per frame."""
        meta = getattr(flow, "metadata", None)
        if isinstance(meta, dict):
            meta[_FLOW_CTX_KEY] = (config, slug, env)

    def _flow_ctx(
        self, flow: http.HTTPFlow,
    ) -> "tuple[Config, str, typing.Mapping[str, str]]":
        """The `(Config, supervise slug, env)` `request()` resolved for this
        flow, so a later hook scans against the calling bottle's policy. Falls
        back to deny-all (empty routes, empty slug) for a flow that never passed
        through `request()` (or a flow object without metadata) — fail-closed, so
        a DLP hook on such a flow is a safe no-op rather than an unscanned pass."""
        meta = getattr(flow, "metadata", None)
        if isinstance(meta, dict):
            ctx = meta.get(_FLOW_CTX_KEY)
            if ctx is not None:
                return ctx
        return Config(routes=()), "", os.environ

    def _request_token(self, flow: http.HTTPFlow) -> str:
        """The per-bottle identity token for this request, from the proxy
        credentials — the delivery mechanism (`HTTPS_PROXY=http://id:token@gw`)
        that clients honor without app changes. Plain-HTTP requests carry
        `Proxy-Authorization` directly; HTTPS bumped requests inherit the token
        captured from their tunnel's CONNECT. Read then stripped so it never
        leaks upstream (also strips the legacy header, if present)."""
        token = _token_from_proxy_auth(
            flow.request.headers.get("Proxy-Authorization", ""))
        flow.request.headers.pop("Proxy-Authorization", None)
        flow.request.headers.pop(IDENTITY_HEADER, None)
        conn = flow.client_conn
        conn_id = getattr(conn, "id", "") if conn is not None else ""
        if not token and conn_id:
            token = self._conn_tokens.get(conn_id, "")
        # Remember the token per connection so a later token-block on this flow
        # can attribute its supervise proposal by (source_ip, identity_token)
        # over RPC — plain-HTTP requests carry it here, HTTPS tunnels captured
        # it at CONNECT (http_connect); both land in `_conn_tokens`.
        if token and conn_id:
            self._conn_tokens[conn_id] = token
        return token

    def http_connect(self, flow: http.HTTPFlow) -> None:
        """Capture the identity token from an HTTPS tunnel's CONNECT (the inner
        bumped requests won't carry `Proxy-Authorization`), keyed by client
        connection, and strip it so it never reaches upstream.

        For `inspect: false` routes, also resolve the policy here to make the
        allowlist decision before the TLS handshake: the tunnel is either
        blocked immediately or marked for passthrough in `_passthrough_conns`
        so `tls_clienthello` skips interception."""
        token = _token_from_proxy_auth(
            flow.request.headers.get("Proxy-Authorization", ""))
        flow.request.headers.pop("Proxy-Authorization", None)
        conn = flow.client_conn
        conn_id = getattr(conn, "id", "") if conn is not None else ""
        if conn_id:
            self._conn_tokens[conn_id] = token

        # Resolve the policy here for all HTTPS connections and stash it so
        # request() reuses it without a second orchestrator round-trip.  For
        # passthrough hosts we also make the allowlist decision now because
        # inner requests never reach request() after the TLS bypass.
        client_ip = conn.peername[0] if conn is not None and conn.peername else ""
        config, slug, env = resolve_client_context(self._resolver, client_ip, token)
        self._stash_flow_ctx(flow, config, slug, env)
        host = flow.request.pretty_host
        route = match_route(config.routes, host)
        if route is not None and not route.inspect:
            decision = decide(config.routes, host, "/", env, deny_reason=config.deny_reason)
            if decision.action == "block":
                flow.response = http.Response.make(
                    403,
                    decision.reason.encode("utf-8"),
                    {"Content-Type": "text/plain; charset=utf-8"},
                )
                return
            if conn_id:
                self._passthrough_conns.add(conn_id)

    def tls_clienthello(self, client_hello: typing.Any) -> None:
        """Skip TLS interception for `inspect: false` routes so the client sees
        the server's real certificate rather than the MITM CA's leaf."""
        conn_id = getattr(client_hello.context.client, "id", "")
        if conn_id in self._passthrough_conns:
            client_hello.ignore_connection = True

    def client_disconnected(self, client: typing.Any) -> None:
        """Drop the per-connection token and passthrough flag when the client
        goes away."""
        conn_id = getattr(client, "id", "")
        self._conn_tokens.pop(conn_id, None)
        self._passthrough_conns.discard(conn_id)

    async def request(self, flow: http.HTTPFlow) -> None:
        config, slug, env = self._request_context(flow)
        request_path, _, query = flow.request.path.partition("?")

        # Introspection ("_egress.local/allowlist") reports the calling bottle's
        # own resolved routes — served after resolution so it reflects this
        # bottle's policy, not a stale global.
        if flow.request.pretty_host == INTROSPECT_HOST:
            self._serve_introspection(flow, request_path, config)
            return

        # DLP outbound scan BEFORE stripping auth — catches tokens the
        # agent tried to smuggle in any header, path, query param, or body.
        # Hostname is included to catch DNS-tunnelling exfiltration attempts.
        # `inspect: false` routes skip scanning entirely (TLS is also not
        # intercepted for HTTPS, so this branch only fires for plain HTTP).
        route = match_route(config.routes, flow.request.pretty_host)
        if route is not None and route.inspect:
            if not await self._handle_outbound_dlp(flow, route, slug, env):
                return
            # The redact policy may have rewritten the request line; recompute
            # the path/query the git checks below rely on.
            request_path, _, query = flow.request.path.partition("?")

        if not self._allow_git_request(flow, config, request_path, query):
            return

        self._apply_route_policy(flow, config, route, request_path, env)

    def _request_context(
        self, flow: http.HTTPFlow,
    ) -> tuple[Config, str, "typing.Mapping[str, str]"]:
        """Resolve one bottle context, reusing the HTTPS CONNECT snapshot."""
        meta = getattr(flow, "metadata", None)
        if isinstance(meta, dict) and _FLOW_CTX_KEY in meta:
            config, slug, env = meta[_FLOW_CTX_KEY]
            self._request_token(flow)
            return config, slug, env
        config, slug, env = self._resolve_flow(flow)
        self._stash_flow_ctx(flow, config, slug, env)
        return config, slug, env

    def _allow_git_request(
        self, flow: http.HTTPFlow, config: Config,
        request_path: str, query: str,
    ) -> bool:
        """Apply the HTTPS Git push/fetch boundary before general routing."""
        reason = git_block_reason(
            config.routes, flow.request.pretty_host, request_path, query,
        )
        if not reason:
            return True
        self._block(flow, reason, ctx=self._req_ctx(flow))
        return False

    def _apply_route_policy(
        self, flow: http.HTTPFlow, config: Config, route: Route | None,
        request_path: str, env: "typing.Mapping[str, str]",
    ) -> None:
        """Strip agent auth, evaluate the route, then inject gateway auth."""
        # Strip agent-set Authorization after DLP scan so smuggled tokens
        # are caught above; the route may inject gateway-owned auth below.
        # Routes with preserve_auth=True pass the header through as-is so the
        # agent's own credentials (e.g. registry bearer tokens) reach the upstream.
        result = evaluate_route_policy(
            config,
            route,
            host=flow.request.pretty_host,
            request_path=request_path,
            method=flow.request.method,
            headers=dict(flow.request.headers),
            env=env,
        )
        if result.strip_authorization:
            flow.request.headers.pop("authorization", None)

        if result.block_reason:
            self._block(flow, result.block_reason, ctx=self._req_ctx(flow))
            return

        if result.inject_authorization is not None:
            flow.request.headers["authorization"] = result.inject_authorization

        if result.log_request:
            self._log_request(flow, env)

    def _block_dlp(self, flow: http.HTTPFlow, result: ScanResult) -> None:
        ctx = self._req_ctx(flow)
        if result.context:
            ctx = {**ctx, "context": result.context}
        self._block(flow, f"egress DLP: {result.reason}", ctx=ctx)

    async def _handle_outbound_dlp(
        self,
        flow: http.HTTPFlow,
        route: Route,
        slug: str,
        env: "typing.Mapping[str, str]",
    ) -> bool:
        """Scan the outbound request and apply the route's on-match policy
        (PRD 0062). `env` is the per-bottle env overlay (process env + this
        bottle's tokens) used for DLP detection. Returns True if the request may
        be forwarded, False if a 403 response has been written to `flow`.

        Loops so the supervise policy can re-scan after each approval — a
        second, un-approved token in the same request is still caught."""
        while True:
            request_path, _, query = flow.request.path.partition("?")
            body = flow.request.get_text(strict=False) or ""
            headers = outbound_scan_headers(route, dict(flow.request.headers))
            scan_text = build_outbound_scan_text(
                flow.request.pretty_host, request_path, query, headers, body,
            )
            # CRLF is scanned only over the request line + headers, never the
            # body (see scan_outbound) — a body is not an injection vector.
            crlf_text = build_outbound_scan_text(
                flow.request.pretty_host, request_path, query, headers, "",
            )
            result = scan_outbound(
                route, scan_text, env,
                safe_tokens=self._safe_tokens_for(slug), crlf_text=crlf_text,
            )
            if result is None or result.severity != "block":
                return True

            policy = route.outbound_on_match or DEFAULT_OUTBOUND_ON_MATCH

            # redact scrubs every detection (tokens and structural CRLF) and
            # forwards; it fails closed only if a match survives the scrub.
            if policy == ON_MATCH_REDACT:
                if self._redact_outbound(flow, route, env):
                    if self._flow_log(flow) >= LOG_BLOCKS:
                        sys.stderr.write(json.dumps({
                            "event": "egress_redacted",
                            "reason": f"egress DLP: {result.reason}",
                            **self._req_ctx(flow),
                        }) + "\n")
                    return True
                self._block(
                    flow,
                    f"egress DLP: {result.reason}; redaction could not remove "
                    "all matches (e.g. a match in the hostname)",
                    ctx=self._req_ctx(flow),
                )
                return False

            # Structural blocks (CRLF, no safelist-able value) cannot be
            # supervised — there is nothing to approve and remember — so under
            # block/supervise they are a hard 403.
            if policy == ON_MATCH_BLOCK or not result.matched:
                self._block_dlp(flow, result)
                return False

            # supervise (default): hold the request for operator approval.
            # Fall back to a hard 403 when supervise isn't wired for the bottle.
            if not self._supervise_available(slug):
                self._block_dlp(flow, result)
                return False
            approved = await self._supervise_token_block(flow, request_path, result, slug, env)
            if not approved:
                return False  # _supervise_token_block wrote the 403 response
            # loop: the approved value is now in safe_tokens; re-scan.

    def _redact_outbound(
        self, flow: http.HTTPFlow, route: Route, env: "typing.Mapping[str, str]",
    ) -> bool:
        """Scrub detected tokens (and CRLF injection sequences) from the mutable
        request surfaces (body, headers, path/query) and re-scan. `env` is the
        per-bottle env overlay. Returns True if the request is now clean; False
        if a block-severity match remains on a surface redaction cannot rewrite
        (the hostname) so the caller fails closed."""
        body = flow.request.get_text(strict=False)
        if body:
            redacted_body = redact_tokens(body, env=env)
            if redacted_body != body:
                flow.request.text = redacted_body
        for name, value in list(flow.request.headers.items()):
            if name.lower() == "host":
                continue  # routing-critical; never a legitimate token
            redacted = strip_crlf(redact_tokens(value, env=env))
            if redacted != value:
                flow.request.headers[name] = redacted
        redacted_path = strip_crlf(redact_tokens(flow.request.path, env=env))
        if redacted_path != flow.request.path:
            flow.request.path = redacted_path

        request_path, _, query = flow.request.path.partition("?")
        new_body = flow.request.get_text(strict=False) or ""
        headers = outbound_scan_headers(route, dict(flow.request.headers))
        scan_text = build_outbound_scan_text(
            flow.request.pretty_host, request_path, query, headers, new_body,
        )
        crlf_text = build_outbound_scan_text(
            flow.request.pretty_host, request_path, query, headers, "",
        )
        result = scan_outbound(route, scan_text, env, crlf_text=crlf_text)
        return result is None or result.severity != "block"

    async def _supervise_token_block(
        self,
        flow: http.HTTPFlow,
        request_path: str,
        result: ScanResult,
        slug: str,
        env: "typing.Mapping[str, str]",
    ) -> bool:
        """Route a token DLP block to the operator's supervisor queue and wait.

        `slug` attributes the proposal to the calling bottle (its own queue +
        safelist) — in the shared gateway this is what keeps one bottle's
        approval from unblocking another's request. `env` is the per-bottle env
        overlay used to redact secrets from the proposal text. Returns True if
        the operator approved (the matched value is added to that bottle's
        safelist and the caller re-scans); False if the request must be blocked
        (a 403 response has been written to `flow`)."""
        host = flow.request.pretty_host
        payload = build_token_allow_payload(
            redact_tokens(host, env=env),
            flow.request.method,
            redact_tokens(request_path, env=env),
            result,
        )
        # Attribute the proposal by (source_ip, identity_token) over the control
        # plane — the data plane no longer opens bot-bottle.db (PRD 0070 / #469).
        # source_ip + token come from this bottle's connection; `slug` is kept
        # only for its own DLP safelist.
        conn = flow.client_conn
        source_ip = conn.peername[0] if conn is not None and conn.peername else ""
        token = self._conn_tokens.get(getattr(conn, "id", ""), "") if conn is not None else ""
        try:
            # The resolver RPC is synchronous (blocking urllib); run it off the
            # event loop so a slow/unreachable orchestrator can't freeze the
            # proxy for every other bottle's traffic (#471 review).
            proposal_id = await asyncio.to_thread(
                self._resolver.propose_supervise,
                source_ip, token,
                tool=TOOL_EGRESS_TOKEN_ALLOW,
                proposed_file=payload,
                justification=_TOKEN_ALLOW_JUSTIFICATION,
            )
        except PolicyResolveError as e:
            sys.stderr.write(
                f"egress: could not queue token-allow proposal: {e}; "
                "blocking request\n"
            )
            proposal_id = None
        if proposal_id is None:
            self._block(flow, f"egress DLP: {result.reason}", ctx=self._req_ctx(flow))
            return False

        sys.stderr.write(json.dumps({
            "event": "egress_token_supervise",
            "reason": f"egress DLP: {result.reason}",
            "proposal": proposal_id,
            **self._req_ctx(flow),
        }) + "\n")

        response = await self._await_token_response(proposal_id, source_ip, token)

        if response is not None and response.get("status") in (
            STATUS_APPROVED, STATUS_MODIFIED,
        ):
            self._safe_tokens_for(slug).add(result.matched)
            if self._flow_log(flow) >= LOG_BLOCKS:
                sys.stderr.write(json.dumps({
                    "event": "egress_token_allowed",
                    "reason": f"egress DLP: {result.reason}",
                    "proposal": proposal_id,
                    **self._req_ctx(flow),
                }) + "\n")
            return True

        if response is None:
            reason = (
                f"egress DLP: {result.reason}; supervisor approval timed out "
                f"after {self._token_allow_timeout:g}s"
            )
        else:
            reason = f"egress DLP: {result.reason}; supervisor rejected the request"
        self._block(flow, reason, ctx=self._req_ctx(flow))
        return False

    async def _await_token_response(
        self,
        proposal_id: str,
        source_ip: str,
        token: str,
    ) -> "dict[str, object] | None":
        """Poll the control plane for the operator's decision without blocking
        the proxy event loop. Returns the terminal `{status, ...}` payload once
        decided, or None on timeout. A transient orchestrator error (or a
        `pending`/`unknown` status) is retried until the deadline, then fails
        closed — the caller blocks the request."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._token_allow_timeout
        while True:
            try:
                # Poll off the event loop — the resolver RPC blocks on urllib,
                # and this loop runs for the whole operator-approval window, so
                # calling it inline would repeatedly freeze all proxy traffic
                # until a decision or timeout (#471 review).
                result = await asyncio.to_thread(
                    self._resolver.poll_supervise, source_ip, token, proposal_id,
                )
            except PolicyResolveError:
                result = None
            if result is not None and result.get("status") in STATUSES:
                return result
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(TOKEN_ALLOW_POLL_INTERVAL_SECONDS)

    def response(self, flow: http.HTTPFlow) -> None:
        """DLP inbound scan on response headers and body, against the calling
        bottle's resolved config (`request()` stashed it — see `_flow_ctx`)."""
        config, _slug, env = self._flow_ctx(flow)
        route = match_route(config.routes, flow.request.pretty_host)
        if route is None or not route.inspect:
            return
        if flow.response is None:
            return
        if config.log >= LOG_FULL:
            self._log_response(flow, env)
        resp_headers = {k.lower(): v for k, v in flow.response.headers.items()}
        body = flow.response.get_text(strict=False) or ""
        if self._inbound_scan_limit and len(body) > self._inbound_scan_limit:
            sys.stderr.write(json.dumps({
                "event": "egress_scan_truncated",
                "host": flow.request.pretty_host,
                "body_bytes": len(body),
                "scan_limit_bytes": self._inbound_scan_limit,
            }) + "\n")
            body = body[:self._inbound_scan_limit]
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
        elif result.severity == "warn" and config.log >= LOG_BLOCKS:
            sys.stderr.write(
                json.dumps({
                    "event": "egress_warn",
                    "reason": f"egress DLP: {result.reason}",
                    **resp_ctx,
                })
                + "\n"
            )

    def websocket_message(self, flow: http.HTTPFlow) -> None:
        """DLP scan on WebSocket frames, against the calling bottle's resolved
        config (see `_flow_ctx`). `request()` resolves and stashes the per-flow
        (config, slug, env) at the upgrade, and every frame reuses it.

        Outbound frames (from_client) are scanned for credential leakage;
        inbound frames are scanned for prompt injection.  On a block the
        entire connection is killed — there is no HTTP response surface to
        write to after the upgrade.
        """
        if flow.websocket is None:  # type: ignore[union-attr]
            return
        config, slug, env = self._flow_ctx(flow)
        route = match_route(config.routes, flow.request.pretty_host)
        if route is None or not route.inspect:
            return
        message = flow.websocket.messages[-1]  # type: ignore[union-attr]
        content = message.content.decode("utf-8", errors="replace")
        if message.from_client:
            # A WebSocket data frame is not an HTTP request line, so CRLF is
            # not an injection vector here — scan only for credential leakage.
            result = scan_outbound(
                route, content, env,
                safe_tokens=self._safe_tokens_for(slug), crlf_text="",
            )
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


def _inbound_scan_limit_from_env(env: "os._Environ[str]") -> int:
    """Read EGRESS_INBOUND_SCAN_LIMIT_BYTES; fall back to the default on an
    unset or invalid value. Returns 0 to disable the cap."""
    raw = env.get("EGRESS_INBOUND_SCAN_LIMIT_BYTES", "").strip()
    if not raw:
        return DEFAULT_INBOUND_SCAN_LIMIT_BYTES
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        sys.stderr.write(
            "egress: invalid EGRESS_INBOUND_SCAN_LIMIT_BYTES="
            f"{raw!r}; using default {DEFAULT_INBOUND_SCAN_LIMIT_BYTES}\n"
        )
        return DEFAULT_INBOUND_SCAN_LIMIT_BYTES
    return value


def _token_allow_timeout_from_env(env: "os._Environ[str]") -> float:
    """Read EGRESS_TOKEN_ALLOW_TIMEOUT_SECONDS; fall back to the default on an
    unset or invalid value (a bad value should not wedge egress at boot)."""
    raw = env.get("EGRESS_TOKEN_ALLOW_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_TOKEN_ALLOW_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        sys.stderr.write(
            "egress: invalid EGRESS_TOKEN_ALLOW_TIMEOUT_SECONDS="
            f"{raw!r}; using default {DEFAULT_TOKEN_ALLOW_TIMEOUT_SECONDS:g}s\n"
        )
        return DEFAULT_TOKEN_ALLOW_TIMEOUT_SECONDS
    return value


addons = [EgressAddon()]
