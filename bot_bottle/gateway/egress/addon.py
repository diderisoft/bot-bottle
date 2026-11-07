"""mitmproxy addon for the egress gateway, loaded by `mitmdump -s
/app/egress_addon.py`."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import sys
import time
import typing

from mitmproxy import http  # type: ignore[import-not-found]  # pylint: disable=import-error

from bot_bottle.contract import blocks
from bot_bottle.contract.identity import IDENTITY_HEADER
from bot_bottle.gateway.audit_client import GatewayAudit
from bot_bottle.gateway.egress.audit_hooks import AUDIT_REFUSED, EgressAudit
from bot_bottle.gateway.egress.firehose import EgressFirehose, FirehoseClient
from bot_bottle.gateway.egress.full_log import FullLog
from bot_bottle.gateway.egress.addon_settings import (
    DEFAULT_INBOUND_SCAN_LIMIT_BYTES,
    _inbound_scan_limit_from_env,
    _token_allow_timeout_from_env,
    _token_allow_ttl_from_env,
)
from bot_bottle.gateway.egress.cookie_jar import CookieJar
from bot_bottle.gateway.egress.dlp_detectors import redact_tokens
from bot_bottle.gateway.egress.dlp_config import (
    DEFAULT_OUTBOUND_ON_MATCH,
    ON_MATCH_BLOCK,
    ON_MATCH_REDACT,
)
from bot_bottle.gateway.egress.context import DENY_UNATTRIBUTED, resolve_client_context
from bot_bottle.gateway.egress.dlp import (
    build_inbound_scan_text,
    build_token_allow_payload,
    scan_inbound,
    scan_outbound,
)
from bot_bottle.gateway.egress.outbound_pipeline import redact_request, scan_request
from bot_bottle.gateway.egress.matching import (
    _strip_port,
    decide,
    match_route,
)
from bot_bottle.gateway.egress.request_pipeline import (
    evaluate_route_policy,
    git_block_reason,
    strip_credential_query,
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
from bot_bottle.contract.supervise import (
    STATUS_APPROVED,
    STATUS_MODIFIED,
    STATUSES,
    TOOL_EGRESS_TOKEN_ALLOW,
)


INTROSPECT_HOST = "_egress.local"


def _norm_host(host: str) -> str:
    return _strip_port(host.strip()).rstrip(".").lower()


def host_mismatch(flow: typing.Any) -> str:
    """Why the host a request names differs from the one mitmproxy connects
    to, or "". Policy keys on that host, so a Host header, `:authority` or SNI
    naming another one would pass as it."""
    target = _norm_host(flow.request.host)
    named = _norm_host(flow.request.pretty_host)
    if named != target:
        return (f"egress: request names host {named!r} but is addressed to "
                f"{target!r}")
    conn = getattr(flow, "client_conn", None)
    sni = getattr(conn, "sni", None) if conn is not None else None
    if sni and _norm_host(sni) != target:
        return (f"egress: TLS SNI {_norm_host(sni)!r} doesn't match the "
                f"addressed host {target!r}")
    return ""

# The orchestrator every request's policy is resolved against. Mandatory.
ORCHESTRATOR_URL_ENV = "BOT_BOTTLE_ORCHESTRATOR_URL"

# Where `request()` stashes the resolved (Config, slug, env) so `response()`
# and `websocket_message()` scan against the same policy without another
# `/resolve`. A flow without it fails closed to deny-all.
_FLOW_CTX_KEY = "bot_bottle_egress_ctx"


def _token_from_proxy_auth(header: str) -> str:
    """The identity token (the password) from `Proxy-Authorization: Basic
    base64(<bottle_id>:<token>)`; "" if malformed, which `/resolve` then
    denies."""
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return ""
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return ""
    _, _, password = decoded.partition(":")
    return password

# Poll interval while awaiting the operator.
TOKEN_ALLOW_POLL_INTERVAL_SECONDS = 0.5

# Guidance attached to every token-allow proposal.
_TOKEN_ALLOW_JUSTIFICATION = (
    "egress DLP blocked an outbound request carrying a detected token. "
    "Approve only if this value is a false positive or a credential this "
    "request legitimately needs; the value is then allowed to this host, "
    "for the life of this bottle's egress proxy."
)


class EgressAddon:
    # Set in __init__; tests build addons via __new__ with a fake.
    _resolver: "PolicyResolver"
    # Class defaults for __new__-built addons; __init__ gives each instance its
    # own.
    _conn_tokens: "dict[str, str]" = {}
    _passthrough_conns: "set[str]" = set()
    _inbound_scan_limit: int = DEFAULT_INBOUND_SCAN_LIMIT_BYTES
    # None: record nothing.
    _audit: "EgressAudit | None" = None
    _firehose: "EgressFirehose | None" = None

    def __init__(self) -> None:
        # Without a policy source the gateway must not start.
        orch_url = os.environ.get(ORCHESTRATOR_URL_ENV, "").strip()
        if not orch_url:
            raise RuntimeError(
                f"{ORCHESTRATOR_URL_ENV} is required: the egress gateway "
                "resolves every request's policy from the orchestrator and has "
                "no static routes file to fall back to."
            )
        self._resolver = PolicyResolver(orch_url)
        audit, firehose = GatewayAudit(orch_url, "egress"), FirehoseClient(orch_url)
        self._audit, self._firehose = EgressAudit(audit), EgressFirehose(firehose)
        self._senders: tuple[GatewayAudit | FirehoseClient, ...] = (audit, firehose)
        # Operator-approved tokens per (bottle, host) with monotonic expiry
        # times. One bottle's approval never passes another's scan, and an
        # approval for one host never lets the value go to another. In memory;
        # only touched from the asyncio loop.
        self._safe_tokens: dict[tuple[str, str], dict[str, float]] = {}
        # What each host set for each bottle via Set-Cookie: the only cookies
        # a request may carry. In memory; only touched from the asyncio loop.
        self._cookies = CookieJar()
        # Per-connection identity token from CONNECT, since bumped HTTPS
        # requests don't repeat it. Cleared on disconnect.
        self._conn_tokens: dict[str, str] = {}
        # `inspect: false` connections, tunnelled without TLS interception.
        # Cleared on disconnect.
        self._passthrough_conns: set[str] = set()
        self._token_allow_timeout = _token_allow_timeout_from_env(os.environ)
        self._token_allow_ttl = _token_allow_ttl_from_env(os.environ)
        self._inbound_scan_limit = _inbound_scan_limit_from_env(os.environ)

    @staticmethod
    def _supervise_available(slug: str) -> bool:
        """True iff the request resolved to a bottle to attribute proposals to."""
        return bool(slug)

    def _safe_tokens_for(self, slug: str, host: str) -> frozenset[str]:
        """Live (non-expired) approved tokens for this bottle and host."""
        now = time.monotonic()
        bucket = self._safe_tokens.get((slug, _norm_host(host)), {})
        return frozenset(t for t, exp in bucket.items() if exp > now)

    def _add_safe_token(self, slug: str, host: str, token: str) -> None:
        """Record an operator-approved token with its expiry time."""
        key = (slug, _norm_host(host))
        if key not in self._safe_tokens:
            self._safe_tokens[key] = {}
        now = time.monotonic()
        bucket = self._safe_tokens[key]
        for stale in [t for t, exp in bucket.items() if exp <= now]:
            del bucket[stale]
        bucket[token] = now + self._token_allow_ttl

    def _serve_introspection(
        self, flow: http.HTTPFlow, path: str, config: Config,
    ) -> None:
        """Serve the calling bottle's own resolved routes."""
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
        """This flow's log level, from its resolved policy."""
        return self._flow_ctx(flow)[0].log

    def _req_ctx(self, flow: http.HTTPFlow) -> dict[str, object]:
        # Redact with this flow's env, so the calling bottle's secrets are
        # scrubbed.
        env = self._flow_ctx(flow)[2]
        return {
            "host": redact_tokens(flow.request.host, env=env),
            "method": flow.request.method,
            "path": redact_tokens(flow.request.path, env=env),
        }

    def _audit_egress(self, flow: http.HTTPFlow, kind: str, outcome: str,
                      reason: str = "", *, hold: bool = True, code: str = "") -> bool:
        """Record a decision (see `audit_hooks`). `hold` (an allowed action)
        honors `audit.delivery: required`; `code` is a block's reason code."""
        if self._audit is None:
            return True
        config, slug, _ = self._flow_ctx(flow)
        return self._audit.record(flow, slug, kind, outcome, reason,
                                  required=hold and config.audit_delivery == "required",
                                  code=code)

    def _first_contact(self, flow: http.HTTPFlow) -> bool:
        if self._audit is None:
            return True
        config, slug, _ = self._flow_ctx(flow)
        return self._audit.first_contact(flow, slug,
                                         required=config.audit_delivery == "required")

    async def _allowed_and_recorded(
        self, flow: http.HTTPFlow, check: "typing.Callable[[], bool]",
    ) -> bool:
        """Run `check` (recording an allowed action), off the event loop when a
        `required` send may block; 403 if the record wasn't written."""
        if self._flow_ctx(flow)[0].audit_delivery == "required":
            recorded = await asyncio.to_thread(check)
        else:
            recorded = check()
        if not recorded:
            self._refuse_unrecorded(flow)
        return recorded

    def _firehose_end(self, flow: http.HTTPFlow, decision: str = "", reason: str = "") -> None:
        if self._firehose is not None:
            self._firehose.end(flow, self._flow_ctx(flow)[1], decision, reason)

    def _refuse_unrecorded(self, flow: http.HTTPFlow) -> None:
        """403 for a `required` action whose record wasn't written."""
        self._firehose_end(flow, "blocked", AUDIT_REFUSED)
        flow.response = http.Response.make(
            403, AUDIT_REFUSED.encode("utf-8"),
            {"Content-Type": "text/plain; charset=utf-8"},
        )

    def _block(
        self,
        flow: http.HTTPFlow,
        reason: str,
        ctx: dict[str, object] | None = None,
        *,
        code: str,
        anomaly: bool = False,
    ) -> None:
        """Answer 403 and record it: a DLP hit as `egress.anomaly`, any other
        refusal as a blocked `egress.decision`, either with its reason `code`."""
        self._audit_egress(flow, "egress.anomaly" if anomaly else "egress.decision",
                           "blocked", reason, hold=False, code=code)
        self._firehose_end(flow, "blocked", reason)
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

    def _full_log(self) -> FullLog:
        """Where `log: 2` bodies go: a capped, rotated 0600 file, never stderr
        (the gateway VM's serial console). Created on first use."""
        log = self.__dict__.get("_full_log_sink")
        if log is None:
            log = self.__dict__["_full_log_sink"] = FullLog.from_env(os.environ)
        return log

    def _log_request(
        self, flow: http.HTTPFlow, env: "typing.Mapping[str, str]",
    ) -> None:
        self._full_log().request(flow, env)

    def _log_response(
        self, flow: http.HTTPFlow, env: "typing.Mapping[str, str]",
    ) -> None:
        self._full_log().response(flow, env)

    def _resolve_flow(
        self, flow: http.HTTPFlow,
    ) -> "tuple[Config, str, typing.Mapping[str, str]]":
        """The calling bottle's `(Config, slug, env)` in one `/resolve`, or
        deny-all with an empty slug when unattributed. `env` overlays the
        bottle's tokens on the process env. The identity token is read and
        stripped."""
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
        """Stash the resolved context for the flow's later hooks."""
        meta = getattr(flow, "metadata", None)
        if isinstance(meta, dict):
            meta[_FLOW_CTX_KEY] = (config, slug, env)

    def _flow_ctx(
        self, flow: http.HTTPFlow,
    ) -> "tuple[Config, str, typing.Mapping[str, str]]":
        """The `(Config, slug, env)` stashed for this flow, or deny-all for a
        flow that never went through `request()`."""
        meta = getattr(flow, "metadata", None)
        if isinstance(meta, dict):
            ctx = meta.get(_FLOW_CTX_KEY)
            if ctx is not None:
                return ctx
        return Config(routes=()), "", os.environ

    def _request_token(self, flow: http.HTTPFlow) -> str:
        """This request's identity token: from `Proxy-Authorization` on plain
        HTTP, or from the tunnel's CONNECT for bumped HTTPS. Read, then
        stripped."""
        token = _token_from_proxy_auth(
            flow.request.headers.get("Proxy-Authorization", ""))
        flow.request.headers.pop("Proxy-Authorization", None)
        flow.request.headers.pop(IDENTITY_HEADER, None)
        conn = flow.client_conn
        conn_id = getattr(conn, "id", "") if conn is not None else ""
        if not token and conn_id:
            token = self._conn_tokens.get(conn_id, "")
        # Remember it per connection to attribute any token-block proposal.
        if token and conn_id:
            self._conn_tokens[conn_id] = token
        return token

    def http_connect(self, flow: http.HTTPFlow) -> None:
        """Capture and strip the identity token from an HTTPS CONNECT, keyed by
        connection. Refuse a host with no route. For `inspect: false` routes,
        decide the allowlist here, before TLS: block, or mark the tunnel for
        passthrough."""
        token = _token_from_proxy_auth(
            flow.request.headers.get("Proxy-Authorization", ""))
        flow.request.headers.pop("Proxy-Authorization", None)
        conn = flow.client_conn
        conn_id = getattr(conn, "id", "") if conn is not None else ""
        if conn_id:
            self._conn_tokens[conn_id] = token

        # Resolve once per HTTPS connection for `request()` to reuse.
        # Passthrough hosts are decided now, since their requests never reach
        # `request()`.
        client_ip = conn.peername[0] if conn is not None and conn.peername else ""
        config, slug, env = resolve_client_context(self._resolver, client_ip, token)
        if not token and config.deny_reason == DENY_UNATTRIBUTED:
            # Ask for the identity: git (`http.proxyAuthMethod=anyauth`) sends
            # the proxy URL's credentials only when challenged.
            flow.response = http.Response.make(
                407, b"bot-bottle: proxy credentials required\n",
                {"Proxy-Authenticate": 'Basic realm="bot-bottle"',
                 "Content-Type": "text/plain; charset=utf-8"},
            )
            return
        self._stash_flow_ctx(flow, config, slug, env)
        mismatch = host_mismatch(flow)
        if mismatch:
            self._block(flow, redact_tokens(mismatch, env=env),
                        ctx=self._req_ctx(flow), code=blocks.REQUEST)
            return
        host = flow.request.host
        route = match_route(config.routes, host)
        # An unlisted host is refused here, before anything is tunnelled.
        if route is None or not route.inspect:
            if self._firehose is not None:  # the tunnel is all there is to record
                self._firehose.begin(flow, env, metadata_only=config.firehose == "metadata")
            decision = decide(config.routes, host, "/", env, deny_reason=config.deny_reason)
            if decision.action == "block":
                self._firehose_end(flow, "blocked", decision.reason)
                self._audit_egress(flow, "egress.decision", "blocked", decision.reason,
                                   hold=False, code=decision.code)
                flow.response = http.Response.make(
                    403,
                    decision.reason.encode("utf-8"),
                    {"Content-Type": "text/plain; charset=utf-8"},
                )
                return
            # Its requests never reach request(); a `required` send blocks
            # here, on first contact only.
            if not self._first_contact(flow):
                self._refuse_unrecorded(flow)
                return
            self._firehose_end(flow, "allowed")
            if conn_id:
                self._passthrough_conns.add(conn_id)

    def tls_clienthello(self, client_hello: typing.Any) -> None:
        """Skip TLS interception for `inspect: false` routes."""
        conn_id = getattr(client_hello.context.client, "id", "")
        if conn_id in self._passthrough_conns:
            client_hello.ignore_connection = True

    def client_disconnected(self, client: typing.Any) -> None:
        """Drop the connection's token and passthrough flag."""
        conn_id = getattr(client, "id", "")
        self._conn_tokens.pop(conn_id, None)
        self._passthrough_conns.discard(conn_id)

    async def request(self, flow: http.HTTPFlow) -> None:
        config, slug, env = self._request_context(flow)
        mismatch = host_mismatch(flow)
        if mismatch:
            self._block(flow, redact_tokens(mismatch, env=env),
                        ctx=self._req_ctx(flow), code=blocks.REQUEST)
            return
        request_path, _, query = flow.request.path.partition("?")

        # Introspection reports the calling bottle's own resolved routes.
        if flow.request.host == INTROSPECT_HOST:
            self._serve_introspection(flow, request_path, config)
            return
        if self._firehose is not None:
            self._firehose.begin(flow, env, metadata_only=config.firehose == "metadata")

        # Outbound DLP before auth is stripped, so tokens smuggled into any
        # header, path, query, body or the hostname are caught. `inspect:
        # false` skips it.
        route = match_route(config.routes, flow.request.host)
        if route is not None and route.inspect:
            if not await self._handle_outbound_dlp(flow, route, slug, env):
                return
            # Redaction may have rewritten the request line.
            request_path, _, query = flow.request.path.partition("?")

        if not self._allow_git_request(flow, config, request_path, query):
            return

        self._apply_route_policy(flow, config, route, request_path, env, slug=slug)
        if flow.response is None:
            await self._allowed_and_recorded(flow, lambda: self._first_contact(flow))

    def _request_context(
        self, flow: http.HTTPFlow,
    ) -> tuple[Config, str, "typing.Mapping[str, str]"]:
        """One bottle context, reusing the CONNECT snapshot."""
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
        """The HTTPS git push/fetch boundary, before general routing."""
        reason = git_block_reason(
            config.routes, flow.request.host, request_path, query,
        )
        if not reason:
            return True
        self._block(flow, reason, ctx=self._req_ctx(flow), code=blocks.ROUTE)
        return False

    def _apply_route_policy(
        self, flow: http.HTTPFlow, config: Config, route: Route | None,
        request_path: str, env: "typing.Mapping[str, str]", *, slug: str,
    ) -> None:
        """Strip agent credentials, evaluate the route, inject gateway auth."""
        # Agent credentials (every CREDENTIAL_HEADERS header, the header the
        # route injects into, and every CREDENTIAL_QUERY_PARAMS parameter) and
        # cookies the host didn't set for this bottle are stripped after the
        # DLP scan and before injection, unless the route sets
        # `preserve_auth`.
        result = evaluate_route_policy(
            config,
            route,
            host=flow.request.host,
            request_path=request_path,
            method=flow.request.method,
            headers=dict(flow.request.headers),
            env=env,
        )
        if result.strip_authorization:
            for name in [n for n in flow.request.headers.keys()
                         if n.lower() in result.strip_headers]:
                flow.request.headers.pop(name, None)
            stripped_path = strip_credential_query(flow.request.path)
            if stripped_path != flow.request.path:
                flow.request.path = stripped_path
            self._filter_cookies(flow, slug)

        if result.block_reason:
            self._block(flow, result.block_reason, ctx=self._req_ctx(flow),
                        code=result.block_code)
            return

        if result.inject_authorization is not None:
            flow.request.headers[result.inject_header] = result.inject_authorization

        if result.log_request:
            self._log_request(flow, env)

    def _cookie_jar(self) -> CookieJar:
        """This addon's jar, created on first use (tests build addons with
        __new__)."""
        jar = self.__dict__.get("_cookies")
        if jar is None:
            jar = self.__dict__["_cookies"] = CookieJar()
        return jar

    def _filter_cookies(self, flow: http.HTTPFlow, slug: str) -> None:
        """Keep only the cookies this host set for this bottle."""
        sent = flow.request.headers.get_all("cookie")
        if not sent:
            return
        kept = self._cookie_jar().filter(slug, flow.request.host, sent)
        flow.request.headers.pop("cookie", None)
        if kept:
            flow.request.headers["cookie"] = kept

    def _fire_canary_event(
        self,
        flow: http.HTTPFlow,
        slug: str,
        result: ScanResult,
        request_path: str,
    ) -> None:
        """Record a canary hit with the orchestrator (best-effort; never raises).
        Called from the asyncio loop, so the RPC runs on a background thread."""
        conn = flow.client_conn
        source_ip = conn.peername[0] if conn is not None and conn.peername else ""
        token = self._conn_tokens.get(getattr(conn, "id", ""), "") if conn is not None else ""
        host = flow.request.host
        full_path = f"{host}{request_path}"
        reason = result.reason

        async def _post() -> None:
            try:
                await asyncio.to_thread(
                    self._resolver.fire_canary,
                    source_ip, token,
                    reason=reason,
                    request_path=full_path,
                )
            except Exception:  # noqa: BLE001 - never block a request
                pass

        asyncio.ensure_future(_post())

    def _block_dlp(self, flow: http.HTTPFlow, result: ScanResult) -> None:
        ctx = self._req_ctx(flow)
        if result.context:
            ctx = {**ctx, "context": result.context}
        # A structural block (CRLF) matched no value; the canary is a secret.
        code = blocks.SECRET if result.matched or result.canary else blocks.REQUEST
        self._block(flow, f"egress DLP: {result.reason}", ctx=ctx, code=code, anomaly=True)

    async def _handle_outbound_dlp(
        self,
        flow: http.HTTPFlow,
        route: Route,
        slug: str,
        env: "typing.Mapping[str, str]",
    ) -> bool:
        """Scan the outbound request and apply the route's on-match policy,
        with `env` (this bottle's overlay). True if the request may go on,
        False if a 403 was written. Loops after each approval so a second token
        is still caught."""
        _MAX_SCAN_ITERATIONS = 20
        for _ in range(_MAX_SCAN_ITERATIONS):
            request_path, _, _ = flow.request.path.partition("?")
            result = scan_request(
                flow.request,
                route,
                env,
                safe_tokens=self._safe_tokens_for(slug, flow.request.host),
            )
            if result is None or result.severity != "block":
                return True

            policy = route.outbound_on_match or DEFAULT_OUTBOUND_ON_MATCH

            # redact scrubs every detection and forwards, failing closed if a
            # match survives. The canary is never redacted.
            if policy == ON_MATCH_REDACT and not result.canary:
                if redact_request(flow.request, route, env):
                    if self._firehose is not None:
                        self._firehose.mark(flow, "redacted")
                    reason = f"egress DLP: {result.reason}; redacted"
                    recorded = await self._allowed_and_recorded(
                        flow, lambda: self._audit_egress(
                            flow, "egress.anomaly", "allowed", reason))
                    if recorded and self._flow_log(flow) >= LOG_BLOCKS:
                        sys.stderr.write(json.dumps({
                            "event": "egress_redacted",
                            "reason": f"egress DLP: {result.reason}",
                            **self._req_ctx(flow),
                        }) + "\n")
                    return recorded
                self._block(
                    flow,
                    f"egress DLP: {result.reason}; redaction could not remove "
                    "all matches (e.g. a match in the hostname)",
                    ctx=self._req_ctx(flow),
                    code=blocks.SECRET,
                    anomaly=True,
                )
                return False

            # Structural blocks (CRLF) and the canary have nothing to approve:
            # hard 403.
            if result.canary or policy == ON_MATCH_BLOCK or not result.matched:
                if result.canary:
                    self._fire_canary_event(flow, slug, result, request_path)
                self._block_dlp(flow, result)
                return False

            # supervise (default): hold for approval, or 403 if supervise isn't
            # wired.
            if not self._supervise_available(slug):
                self._block_dlp(flow, result)
                return False
            approved = await self._supervise_token_block(flow, request_path, result, slug, env)
            if not approved:
                return False  # _supervise_token_block wrote the 403
            # the approved value is now safelisted; re-scan

        # Iteration limit reached: fail closed.
        self._block(
            flow,
            "egress DLP: scan/approve cycle exceeded limit; request blocked",
            ctx=self._req_ctx(flow),
            code=blocks.SECRET,
            anomaly=True,
        )
        return False

    async def _supervise_token_block(
        self,
        flow: http.HTTPFlow,
        request_path: str,
        result: ScanResult,
        slug: str,
        env: "typing.Mapping[str, str]",
    ) -> bool:
        """Queue a token block for the operator and wait. `slug` and the host
        scope the safelist entry; `env` redacts secrets from
        the proposal. True if approved (the value is safelisted); False if
        blocked (403 written)."""
        host = flow.request.host
        payload = build_token_allow_payload(
            redact_tokens(host, env=env),
            flow.request.method,
            redact_tokens(request_path, env=env),
            result,
        )
        # Attribute by (source_ip, identity_token) over the control plane;
        # `slug` is only for the safelist.
        conn = flow.client_conn
        source_ip = conn.peername[0] if conn is not None and conn.peername else ""
        token = self._conn_tokens.get(getattr(conn, "id", ""), "") if conn is not None else ""
        try:
            # The RPC blocks; run it off the event loop so a slow orchestrator
            # can't stall other bottles.
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
            self._block(flow, f"egress DLP: {result.reason}", ctx=self._req_ctx(flow),
                        code=blocks.SECRET, anomaly=True)
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
            self._add_safe_token(slug, host, result.matched)
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
            code = blocks.SECRET
        else:
            reason = f"egress DLP: {result.reason}; supervisor rejected the request"
            code = blocks.DENIED
        self._block(flow, reason, ctx=self._req_ctx(flow), code=code, anomaly=True)
        return False

    async def _await_token_response(
        self,
        proposal_id: str,
        source_ip: str,
        token: str,
    ) -> "dict[str, object] | None":
        """Poll for the operator's decision off the event loop. Returns the
        terminal payload, or None at the deadline. Errors and
        `pending`/`unknown` are retried until then."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._token_allow_timeout
        while True:
            try:
                # Off the event loop for the whole approval window.
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
        """Inbound DLP on response headers and body, against the flow's stashed
        policy."""
        self._firehose_end(flow)  # once per flow; a block already ended it
        config, slug, env = self._flow_ctx(flow)
        route = match_route(config.routes, flow.request.host)
        if route is None or not route.inspect:
            return
        if flow.response is None:
            return
        self._cookie_jar().record(
            slug, flow.request.host, flow.response.headers.get_all("set-cookie"))
        if config.log >= LOG_FULL:
            self._log_response(flow, env)
        resp_headers = {k.lower(): v for k, v in flow.response.headers.items()}
        body = flow.response.get_text(strict=False) or ""
        if self._inbound_scan_limit and len(body) > self._inbound_scan_limit:
            sys.stderr.write(json.dumps({
                "event": "egress_scan_truncated",
                "host": flow.request.host,
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
            self._block(flow, f"egress DLP: {result.reason}", ctx=resp_ctx,
                        code=blocks.INBOUND, anomaly=True)
        elif result.severity == "warn" and config.log >= LOG_BLOCKS:
            sys.stderr.write(
                json.dumps({
                    "event": "egress_warn",
                    "reason": f"egress DLP: {result.reason}",
                    **resp_ctx,
                })
                + "\n"
            )

    def load(self, loader: typing.Any) -> None:
        """mitmproxy loaded the addon: refuse raw TCP, and start the audit and
        firehose senders (not in __init__, so importing starts no threads)."""
        del loader
        # Non-HTTP bytes after a CONNECT would otherwise be tunnelled to the
        # host as raw TCP, unscanned. `inspect: false` passthrough is unaffected.
        from mitmproxy import ctx  # type: ignore[import-not-found]  # pylint: disable=import-error,import-outside-toplevel
        ctx.options.update(rawtcp=False)
        for sender in self._senders:
            sender.start()

    def error(self, flow: http.HTTPFlow) -> None:
        """A request that failed upstream still gets its record."""
        self._firehose_end(flow, "failed", str(getattr(flow, "error", "") or ""))

    def websocket_message(self, flow: http.HTTPFlow) -> None:
        """DLP on WebSocket frames against the policy stashed at the upgrade:
        outbound for leaks, inbound for prompt injection. A block kills the
        connection.
        """
        if flow.websocket is None:  # type: ignore[union-attr]
            return
        config, slug, env = self._flow_ctx(flow)
        route = match_route(config.routes, flow.request.host)
        if route is None or not route.inspect:
            return
        message = flow.websocket.messages[-1]  # type: ignore[union-attr]
        content = message.content.decode("utf-8", errors="replace")
        if message.from_client:
            # Frames aren't request lines, so no CRLF check.
            result = scan_outbound(
                route, content, env,
                safe_tokens=self._safe_tokens_for(slug, flow.request.host),
                crlf_text="",
            )
            if result is not None and result.severity == "block":
                sys.stderr.write(f"egress DLP: {result.reason}\n")
                self._audit_egress(flow, "egress.anomaly", "blocked",
                                   f"egress DLP: {result.reason} (websocket)", hold=False,
                                   code=blocks.SECRET)
                flow.kill()  # type: ignore[union-attr]
        else:
            result = scan_inbound(route, content)
            if result is not None:
                if result.severity == "block":
                    sys.stderr.write(f"egress DLP: {result.reason}\n")
                    self._audit_egress(flow, "egress.anomaly", "blocked",
                                       f"egress DLP: {result.reason} (websocket)", hold=False,
                                       code=blocks.INBOUND)
                    flow.kill()  # type: ignore[union-attr]
                elif result.severity == "warn":
                    sys.stderr.write(f"egress DLP warn: {result.reason}\n")


addons = [EgressAddon()]
