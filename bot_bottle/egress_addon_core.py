"""Pure logic for the egress mitmproxy addon (PRD 0017, PRD 0053).

Split out of `egress_addon.py` so the host's unit tests can
exercise the parse + decision functions without depending on the
`mitmproxy` package. The companion module wraps these with the
`mitmproxy.http.HTTPFlow` API and is loaded inside the gateway
container.

Imports: stdlib + sibling package modules (`yaml_subset`,
`egress_dlp_config`). Available in the gateway via the installed
`bot_bottle` package (see `Dockerfile.gateway`)."""

from __future__ import annotations

import re
import typing
from dataclasses import dataclass

from .yaml_subset import YamlSubsetError, parse_yaml_subset

# DLP detector-config parsing lives in a sibling module. Re-exported below
# so existing `from egress_addon_core import ON_MATCH_*` callers keep working.
from .egress_dlp_config import (
    DEFAULT_OUTBOUND_ON_MATCH,
    INBOUND_DETECTOR_NAMES,
    ON_MATCH_BLOCK,
    ON_MATCH_REDACT,
    ON_MATCH_SUPERVISE,
    OUTBOUND_DETECTOR_NAMES,
    OUTBOUND_ON_MATCH_VALUES,
    parse_dlp_block,
)


# ---------------------------------------------------------------------------
# Match types (Gateway API HTTPRoute vocabulary, PRD 0053)
# ---------------------------------------------------------------------------

PATH_MATCH_TYPES = ("exact", "prefix", "regex")
HEADER_MATCH_TYPES = ("exact", "regex")

VALID_METHODS = frozenset({
    "GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE",
    "CONNECT",
})


@dataclass(frozen=True)
class PathMatch:
    type: str   # "exact" | "prefix" | "regex"
    value: str
    compiled: re.Pattern[str] | None = None


@dataclass(frozen=True)
class HeaderMatch:
    name: str
    value: str
    type: str = "exact"   # "exact" | "regex"
    compiled: re.Pattern[str] | None = None


@dataclass(frozen=True)
class MatchEntry:
    paths: tuple[PathMatch, ...] = ()
    methods: tuple[str, ...] = ()
    headers: tuple[HeaderMatch, ...] = ()


@dataclass(frozen=True)
class Route:
    host: str
    matches: tuple[MatchEntry, ...] = ()
    auth_scheme: str = ""
    token_env: str = ""
    git_fetch: bool = False
    outbound_detectors: tuple[str, ...] | None = None
    inbound_detectors: tuple[str, ...] | None = None
    # "" means unset → DEFAULT_OUTBOUND_ON_MATCH. See OUTBOUND_ON_MATCH_VALUES.
    outbound_on_match: str = ""
    preserve_auth: bool = False
    # dlp: false — skip all scanning; HTTPS flows tunnel without TLS interception.
    dlp_passthrough: bool = False


LOG_OFF = 0    # no logging
LOG_BLOCKS = 1  # log block/warn events with request context
LOG_FULL = 2    # log block/warn events + full request and response bodies


@dataclass(frozen=True)
class Config:
    routes: tuple[Route, ...]
    log: int = LOG_OFF
    # Why this Config is a deny-all, when it is one for a reason *other* than
    # the bottle's own policy genuinely not listing the host. A deny-all is
    # indistinguishable from "policy loaded, host not allowed" at the decision
    # point — both are simply "no matching route" — so without this the
    # operator sees `host X is not in the allowlist` and goes hunting for a
    # missing route that was never the problem. Empty for a normally-parsed
    # policy; `decide` prefers it over the allowlist wording when set.
    deny_reason: str = ""


@dataclass(frozen=True)
class Decision:
    action: str  # "forward" or "block"
    reason: str = ""
    inject_authorization: str | None = None


@dataclass(frozen=True)
class ScanResult:
    severity: str   # "block" or "warn"
    reason: str
    location: str = ""  # where the match was found, e.g. "body", "authorization header"
    context: str = ""   # surrounding text with the match replaced by REDACT
    # Raw substring the detector matched. Used inside the gateway to key the
    # supervisor-approved "safe tokens" set (PRD 0062); never logged or written
    # to a proposal file. Empty for structural detectors (CRLF) that carry no
    # safelist-able value.
    matched: str = ""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_path_match(idx: int, j: int, raw: object) -> PathMatch:
    label = f"route[{idx}] matches paths[{j}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{label}: must be an object")
    raw_dict: dict[str, object] = typing.cast(dict[str, object], raw)
    ptype = raw_dict.get("type", "prefix")
    if not isinstance(ptype, str) or ptype not in PATH_MATCH_TYPES:
        raise ValueError(
            f"{label}: 'type' must be one of {', '.join(PATH_MATCH_TYPES)} "
            f"(got {ptype!r})"
        )
    value = raw_dict.get("value")
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}: 'value' must be a non-empty string")
    if ptype in ("exact", "prefix") and not value.startswith("/"):
        raise ValueError(
            f"{label}: value {value!r} must start with '/' for "
            f"type {ptype!r}"
        )
    compiled: re.Pattern[str] | None = None
    if ptype == "regex":
        try:
            compiled = re.compile(value)
        except re.error as e:
            raise ValueError(
                f"{label}: regex {value!r} failed to compile: {e}"
            ) from e
    for k in raw_dict:
        if k not in ("type", "value"):
            raise ValueError(f"{label}: unknown key {k!r}")
    return PathMatch(type=ptype, value=value, compiled=compiled)


def _parse_header_match(idx: int, j: int, raw: object) -> HeaderMatch:
    label = f"route[{idx}] matches headers[{j}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{label}: must be an object")
    raw_dict: dict[str, object] = typing.cast(dict[str, object], raw)
    name = raw_dict.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{label}: 'name' must be a non-empty string")
    value = raw_dict.get("value")
    if not isinstance(value, str):
        raise ValueError(f"{label}: 'value' must be a string")
    htype = raw_dict.get("type", "exact")
    if not isinstance(htype, str) or htype not in HEADER_MATCH_TYPES:
        raise ValueError(
            f"{label}: 'type' must be one of {', '.join(HEADER_MATCH_TYPES)} "
            f"(got {htype!r})"
        )
    compiled: re.Pattern[str] | None = None
    if htype == "regex":
        try:
            compiled = re.compile(value)
        except re.error as e:
            raise ValueError(
                f"{label}: regex {value!r} failed to compile: {e}"
            ) from e
    for k in raw_dict:
        if k not in ("name", "value", "type"):
            raise ValueError(f"{label}: unknown key {k!r}")
    return HeaderMatch(name=name, value=value, type=htype, compiled=compiled)


def _parse_match_entry(idx: int, k: int, raw: object) -> MatchEntry:
    label = f"route[{idx}] matches[{k}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{label}: must be an object")
    raw_dict: dict[str, object] = typing.cast(dict[str, object], raw)

    paths: tuple[PathMatch, ...] = ()
    paths_raw = raw_dict.get("paths")
    if paths_raw is not None:
        if not isinstance(paths_raw, list):
            raise ValueError(f"{label}: 'paths' must be a list")
        paths_list = typing.cast(list[object], paths_raw)
        paths = tuple(_parse_path_match(idx, j, p) for j, p in enumerate(paths_list))

    methods: tuple[str, ...] = ()
    methods_raw = raw_dict.get("methods")
    if methods_raw is not None:
        if not isinstance(methods_raw, list):
            raise ValueError(f"{label}: 'methods' must be a list")
        methods_list = typing.cast(list[object], methods_raw)
        normalised: list[str] = []
        for j, m in enumerate(methods_list):
            if not isinstance(m, str):
                raise ValueError(f"{label}: methods[{j}] must be a string")
            upper = m.upper()
            if upper not in VALID_METHODS:
                raise ValueError(
                    f"{label}: methods[{j}] {m!r} is not a valid HTTP method"
                )
            normalised.append(upper)
        methods = tuple(normalised)

    headers: tuple[HeaderMatch, ...] = ()
    headers_raw = raw_dict.get("headers")
    if headers_raw is not None:
        if not isinstance(headers_raw, list):
            raise ValueError(f"{label}: 'headers' must be a list")
        headers_list = typing.cast(list[object], headers_raw)
        headers = tuple(
            _parse_header_match(idx, j, h) for j, h in enumerate(headers_list)
        )

    for key in raw_dict:
        if key not in ("paths", "methods", "headers"):
            raise ValueError(f"{label}: unknown key {key!r}")

    return MatchEntry(paths=paths, methods=methods, headers=headers)


def parse_routes(payload: object) -> tuple[Route, ...]:
    if not isinstance(payload, dict):
        raise ValueError("routes payload: top-level must be an object")
    payload_dict: dict[str, object] = typing.cast(dict[str, object], payload)
    raw: object = payload_dict.get("routes")
    if not isinstance(raw, list):
        raise ValueError("routes payload: 'routes' must be a list")
    raw_list: list[object] = typing.cast(list[object], raw)
    out: list[Route] = []
    for i, r in enumerate(raw_list):
        out.append(_parse_one(i, r))
    return tuple(out)


def _parse_one(idx: int, raw: object) -> Route:
    label = f"route[{idx}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{label}: must be an object (got {type(raw).__name__})")
    raw_dict: dict[str, object] = typing.cast(dict[str, object], raw)
    host: object = raw_dict.get("host")
    if not isinstance(host, str) or not host:
        raise ValueError(f"{label}: 'host' must be a non-empty string")

    # matches
    matches: tuple[MatchEntry, ...] = ()
    matches_raw = raw_dict.get("matches")
    if matches_raw is not None:
        if not isinstance(matches_raw, list):
            raise ValueError(f"{label} ({host}): 'matches' must be a list")
        matches_list = typing.cast(list[object], matches_raw)
        matches = tuple(
            _parse_match_entry(idx, k, m) for k, m in enumerate(matches_list)
        )

    # auth (unchanged wire format)
    auth_scheme: object = raw_dict.get("auth_scheme", "")
    token_env: object = raw_dict.get("token_env", "")
    if not isinstance(auth_scheme, str):
        raise ValueError(f"{label} ({host}): 'auth_scheme' must be a string")
    if not isinstance(token_env, str):
        raise ValueError(f"{label} ({host}): 'token_env' must be a string")
    if bool(auth_scheme) != bool(token_env):
        raise ValueError(
            f"{label} ({host}): 'auth_scheme' and 'token_env' must be both "
            f"set or both empty (got auth_scheme={auth_scheme!r}, "
            f"token_env={token_env!r})"
        )

    # git-over-HTTPS policy
    git_fetch = False
    git_raw = raw_dict.get("git")
    if git_raw is not None:
        if not isinstance(git_raw, dict):
            raise ValueError(f"{label} ({host}): 'git' must be an object")
        git_dict: dict[str, object] = typing.cast(dict[str, object], git_raw)
        fetch_raw = git_dict.get("fetch", False)
        if fetch_raw is True or fetch_raw is False:
            git_fetch = fetch_raw
        else:
            raise ValueError(f"{label} ({host}): 'git.fetch' must be a boolean")
        for k in git_dict:
            if k != "fetch":
                raise ValueError(
                    f"{label} ({host}): git has unknown key {k!r}; "
                    "accepted key is 'fetch'"
                )

    # dlp detectors
    outbound_detectors, inbound_detectors, outbound_on_match, dlp_passthrough = parse_dlp_block(
        idx, host, raw_dict,
    )

    preserve_auth_raw = raw_dict.get("preserve_auth", False)
    if preserve_auth_raw is not True and preserve_auth_raw is not False:
        raise ValueError(
            f"{label} ({host}): 'preserve_auth' must be a boolean"
        )
    preserve_auth: bool = preserve_auth_raw

    for k in raw_dict:
        if k not in ("host", "matches", "auth_scheme", "token_env", "dlp", "git", "preserve_auth"):
            raise ValueError(
                f"{label} ({host}): unknown key {k!r}; accepted keys "
                f"are 'host', 'matches', 'auth_scheme', 'token_env', 'dlp', 'git', 'preserve_auth'"
            )

    return Route(
        host=host,
        matches=matches,
        auth_scheme=auth_scheme,
        token_env=token_env,
        git_fetch=git_fetch,
        outbound_detectors=outbound_detectors,
        inbound_detectors=inbound_detectors,
        outbound_on_match=outbound_on_match,
        preserve_auth=preserve_auth,
        dlp_passthrough=dlp_passthrough,
    )


def _path_match_to_dict(pm: PathMatch) -> dict[str, object]:
    d: dict[str, object] = {"value": pm.value}
    if pm.type != "prefix":
        d["type"] = pm.type
    return d


def _header_match_to_dict(hm: HeaderMatch) -> dict[str, object]:
    d: dict[str, object] = {"name": hm.name, "value": hm.value}
    if hm.type != "exact":
        d["type"] = hm.type
    return d


def _match_entry_to_dict(me: MatchEntry) -> dict[str, object]:
    d: dict[str, object] = {}
    if me.paths:
        d["paths"] = [_path_match_to_dict(p) for p in me.paths]
    if me.methods:
        d["methods"] = list(me.methods)
    if me.headers:
        d["headers"] = [_header_match_to_dict(h) for h in me.headers]
    return d


def route_to_yaml_dict(r: Route) -> dict[str, object]:
    """Serialize a Route to YAML-schema-compatible dict.

    Uses the same field names the YAML parser accepts, so the output
    can be round-tripped directly into an `allow` or `egress-block`
    proposal without translation. Fields that are empty/default are
    omitted so the agent doesn't copy irrelevant keys."""
    d: dict[str, object] = {"host": r.host}
    if r.auth_scheme:
        d["auth_scheme"] = r.auth_scheme
        d["token_env"] = r.token_env
    if r.matches:
        d["matches"] = [_match_entry_to_dict(m) for m in r.matches]
    if r.git_fetch:
        d["git"] = {"fetch": True}
    if r.dlp_passthrough:
        d["dlp"] = False
    else:
        dlp: dict[str, object] = {}
        if r.outbound_detectors is not None:
            dlp["outbound_detectors"] = list(r.outbound_detectors)
        if r.inbound_detectors is not None:
            dlp["inbound_detectors"] = list(r.inbound_detectors)
        if r.outbound_on_match:
            dlp["outbound_on_match"] = r.outbound_on_match
        if dlp:
            d["dlp"] = dlp
    if r.preserve_auth:
        d["preserve_auth"] = True
    return d


def parse_config(payload: object) -> "Config":
    """Parse a full egress config payload (top-level log level + routes)."""
    if not isinstance(payload, dict):
        raise ValueError("routes payload: top-level must be an object")
    payload_dict: dict[str, object] = typing.cast(dict[str, object], payload)

    log_raw: object = payload_dict.get("log", LOG_OFF)
    if log_raw is True or log_raw is False or not isinstance(log_raw, int) \
            or log_raw not in (LOG_OFF, LOG_BLOCKS, LOG_FULL):
        raise ValueError(
            f"routes payload: 'log' must be {LOG_OFF}, {LOG_BLOCKS}, or {LOG_FULL}"
        )

    routes = parse_routes(payload)
    return Config(routes=routes, log=log_raw)


def load_config(text: str) -> "Config":
    """Parse YAML text → Config (routes + log flag)."""
    try:
        payload = parse_yaml_subset(text)
    except YamlSubsetError as e:
        raise ValueError(f"routes payload: invalid YAML: {e}") from e
    return parse_config(payload)


class PolicyResolverLike(typing.Protocol):
    """The bit of `policy_resolver.PolicyResolver` this module needs — kept a
    Protocol so egress_addon_core stays free of that import."""

    def resolve(self, source_ip: str, identity_token: str = ...) -> "str | None":
        ...


# Deny-all explanations. Each names the *actual* failure so an operator isn't
# sent looking for a missing egress route when the bottle never had a policy
# to begin with — the failure mode that made a bricked registration read like
# a misconfigured allowlist.
DENY_UNATTRIBUTED = (
    "egress: this request was not attributed to any bottle, so no egress "
    "policy applies and every host is denied. Either the bottle's registry "
    "row is missing/ambiguous (torn down, or another bottle claimed its "
    "source IP), or the request carried no matching identity token — check "
    "that the caller's proxy URL includes it. This is not an allowlist problem."
)
DENY_UNPARSEABLE = (
    "egress: this bottle's egress policy could not be parsed, so it is being "
    "treated as deny-all. Fix the bottle's egress.routes; every host is denied "
    "until it loads."
)
DENY_RESOLVER_ERROR = (
    "egress: the orchestrator could not be reached to resolve this bottle's "
    "egress policy, so every host is denied (fail-closed). Check that the "
    "control plane is up; this is not an allowlist problem."
)


def _config_from_policy(policy: "str | None") -> "Config":
    """Parse a resolved policy blob into a Config, fail-closed: None / empty /
    unparseable all become a deny-all Config (no routes → every request
    blocked). Each deny-all carries the reason it is one, so the block message
    names the real fault instead of blaming the allowlist."""
    if not policy:
        return Config(routes=(), deny_reason=DENY_UNATTRIBUTED)
    try:
        return load_config(policy)
    except ValueError:
        return Config(routes=(), deny_reason=DENY_UNPARSEABLE)


def resolve_client_config(
    resolver: PolicyResolverLike, client_ip: str, identity_token: str = ""
) -> "Config":
    """The calling client's egress Config, resolved from the orchestrator via
    `resolver` and parsed — **fail-closed**. An unattributed client (None), a
    resolver error, or an unparseable policy all yield a deny-all Config (no
    routes → every request blocked). A compromised, absent, or confused
    orchestrator must never *widen* a bottle's egress."""
    try:
        policy = resolver.resolve(client_ip, identity_token)
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return Config(routes=(), deny_reason=DENY_RESOLVER_ERROR)
    return _config_from_policy(policy)


class ContextResolverLike(typing.Protocol):
    """The bit of `policy_resolver.PolicyResolver` `resolve_client_context`
    needs — one round-trip returning policy, bottle id, and auth tokens."""

    def resolve_policy_and_bottle_id(
        self, source_ip: str, identity_token: str = ...,
    ) -> "tuple[str | None, str | None, dict[str, str]]":
        ...


def resolve_client_context(
    resolver: ContextResolverLike, client_ip: str, identity_token: str = "",
) -> "tuple[Config, str, dict[str, str]]":
    """The calling client's `(Config, bottle_id, tokens)` in one round-trip —
    **fail-closed**. The Config follows `resolve_client_config`'s deny-all
    rules; the bottle id is `""` whenever unattributed or the orchestrator
    errored (caller treats as "supervise unavailable", never another bottle's
    queue); `tokens` are the per-bottle upstream auth values the addon injects.
    One `/resolve` keys the egress policy, the supervise queue + safelist, and
    auth injection."""
    try:
        policy, bottle_id, tokens = resolver.resolve_policy_and_bottle_id(
            client_ip, identity_token,
        )
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return Config(routes=(), deny_reason=DENY_RESOLVER_ERROR), "", {}
    return _config_from_policy(policy), (bottle_id or ""), tokens


# ---------------------------------------------------------------------------
# Match evaluation
# ---------------------------------------------------------------------------

def _path_matches(pm: PathMatch, request_path: str) -> bool:
    if pm.type == "exact":
        return request_path == pm.value
    if pm.type == "prefix":
        if request_path == pm.value:
            return True
        if not pm.value.endswith("/"):
            return request_path.startswith(pm.value + "/")
        return request_path.startswith(pm.value)
    if pm.type == "regex" and pm.compiled is not None:
        return pm.compiled.search(request_path) is not None
    return False


def _entry_matches(
    entry: MatchEntry,
    request_path: str,
    request_method: str,
    request_headers: typing.Mapping[str, str],
) -> bool:
    """All predicates within a MatchEntry are ANDed."""
    if entry.paths:
        if not any(_path_matches(pm, request_path) for pm in entry.paths):
            return False
    if entry.methods:
        if request_method.upper() not in entry.methods:
            return False
    if entry.headers:
        for hm in entry.headers:
            header_val = request_headers.get(hm.name.lower())
            if header_val is None:
                return False
            if hm.type == "exact":
                if header_val != hm.value:
                    return False
            elif hm.type == "regex" and hm.compiled is not None:
                if not hm.compiled.search(header_val):
                    return False
    return True


def evaluate_matches(
    route: Route,
    request_path: str,
    request_method: str = "GET",
    request_headers: typing.Mapping[str, str] | None = None,
) -> bool:
    """Return True if the request matches this route's match entries.
    Empty matches tuple means all requests match (bare-pass route)."""
    if not route.matches:
        return True
    hdrs: typing.Mapping[str, str] = request_headers or {}
    return any(
        _entry_matches(entry, request_path, request_method, hdrs)
        for entry in route.matches
    )


# ---------------------------------------------------------------------------
# Git push detection (unchanged)
# ---------------------------------------------------------------------------

def is_git_push_request(path: str, query: str) -> bool:
    if path.endswith("/git-receive-pack"):
        return True
    if path.endswith("/info/refs"):
        for pair in query.split("&"):
            k, _, v = pair.partition("=")
            if k == "service" and v == "git-receive-pack":
                return True
    return False


def is_git_fetch_request(path: str, query: str) -> bool:
    if path.endswith("/git-upload-pack"):
        return True
    if path.endswith("/info/refs"):
        for pair in query.split("&"):
            k, _, v = pair.partition("=")
            if k == "service" and v == "git-upload-pack":
                return True
    return False


# ---------------------------------------------------------------------------
# Route lookup + decision
# ---------------------------------------------------------------------------

def match_route(
    routes: typing.Sequence[Route],
    request_host: str,
) -> Route | None:
    target = request_host.lower()
    for r in routes:
        if r.host.lower() == target:
            return r
    return None


def decide(
    routes: typing.Sequence[Route],
    request_host: str,
    request_path: str,
    environ: typing.Mapping[str, str],
    *,
    request_method: str = "GET",
    request_headers: typing.Mapping[str, str] | None = None,
    deny_reason: str = "",
) -> Decision:
    """`deny_reason` is `Config.deny_reason`: when the deny-all came from a
    missing/unparseable policy rather than the bottle's own allowlist, report
    that instead of implying a route is merely absent."""
    route = match_route(routes, request_host)
    if route is None:
        return Decision(
            action="block",
            reason=deny_reason or (
                f"egress: host {request_host!r} is not in the "
                f"bottle's egress.routes allowlist. Declare a "
                f"route for it or remove the request."
            ),
        )

    if not evaluate_matches(route, request_path, request_method, request_headers):
        return Decision(
            action="block",
            reason=(
                f"egress: request {request_method} {request_path!r} "
                f"does not match any entry in matches for "
                f"{route.host!r}"
            ),
        )

    if route.auth_scheme and route.token_env:
        token = environ.get(route.token_env, "")
        if not token:
            return Decision(
                action="block",
                reason=(
                    f"egress: route for {route.host!r} declared auth "
                    f"but env var {route.token_env!r} is unset"
                ),
            )
        return Decision(
            action="forward",
            inject_authorization=f"{route.auth_scheme} {token}",
        )

    return Decision(action="forward")


def decide_git_fetch(
    routes: typing.Sequence[Route],
    request_host: str,
) -> Decision:
    route = match_route(routes, request_host)
    if route is not None and route.git_fetch:
        return Decision(action="forward")
    return Decision(
        action="block",
        reason=(
            "egress: git fetch/clone over HTTPS is not allowed by default; "
            "use git-gate for declared repos or set "
            "egress.routes[].git.fetch=true for explicit read-only "
            "HTTPS Git access."
        ),
    )


# ---------------------------------------------------------------------------
# DLP scan dispatch (PRD 0053)
# ---------------------------------------------------------------------------

def build_outbound_scan_text(
    host: str,
    path: str,
    query: str,
    headers: typing.Mapping[str, str],
    body: str,
) -> str:
    """Assemble all outbound request surfaces into one string for DLP scanning.

    Covers hostname (DNS tunnelling), path, query params, all headers, body.
    """
    parts: list[str] = [host, path]
    if query:
        parts.append(query)
    for name, value in headers.items():
        parts.append(f"{name}: {value}")
    if body:
        parts.append(body)
    return "\n".join(parts)


def outbound_scan_headers(
    route: Route,
    headers: typing.Mapping[str, str],
) -> dict[str, str]:
    """Return request headers that should be included in outbound DLP.

    Routes that inject gateway-owned auth always strip the agent's
    Authorization header before forwarding. Scanning that header first
    creates false positives for provider clients that insist on sending
    their own bearer-shaped placeholder, while still not changing what
    reaches the upstream.
    """
    out: dict[str, str] = {}
    skip_auth = bool(route.auth_scheme and route.token_env)
    for name, value in headers.items():
        if skip_auth and name.lower() == "authorization":
            continue
        out[name] = value
    return out


def build_inbound_scan_text(
    headers: typing.Mapping[str, str],
    body: str,
) -> str:
    """Assemble inbound response surfaces into one string for DLP scanning.

    Covers all response headers plus body.
    """
    parts: list[str] = []
    for name, value in headers.items():
        parts.append(f"{name}: {value}")
    if body:
        parts.append(body)
    return "\n".join(parts)


def _detector_enabled(
    configured: tuple[str, ...] | None,
    name: str,
) -> bool:
    """Check if a named detector is enabled for a route direction.
    None means all enabled; empty tuple means all disabled."""
    if configured is None:
        return True
    return name in configured


def scan_outbound(
    route: Route,
    body: str | bytes,
    environ: typing.Mapping[str, str],
    *,
    safe_tokens: typing.AbstractSet[str] | None = None,
    crlf_text: str | None = None,
) -> ScanResult | None:
    if route.dlp_passthrough:
        return None
    # Lazy import to avoid circular deps and keep dlp_detectors optional
    # at import time (the gateway copies it flat alongside this file).
    try:
        from dlp_detectors import (  # type: ignore[import-not-found]
            scan_crlf_injection,
            scan_entropy,
            scan_known_secrets,
            scan_token_patterns,
        )
    except ImportError:  # pragma: no cover - host-side path
        from .dlp_detectors import (  # type: ignore[import-not-found]
            scan_crlf_injection,
            scan_entropy,
            scan_known_secrets,
            scan_token_patterns,
        )

    # Binary bodies: latin-1 is a bijective byte↔codepoint mapping that
    # preserves every byte value, so ASCII-range secret strings remain
    # findable by str.find / regex.  Prefer strict UTF-8 for valid text bodies.
    if isinstance(body, bytes):
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = body.decode("latin-1")
    else:
        text = body

    # CRLF injection is only an attack in the request line + headers, never the
    # body: an HTTP body is delimited by Content-Length, so CRLF bytes there
    # cannot split the request. Scanning the body produces false positives on
    # legitimate form-encoded / multi-line content. Callers pass the
    # body-excluded surfaces as `crlf_text`; `None` falls back to the full text
    # for backward-compatible callers (host-side tests, websocket frames).
    crlf_target = text if crlf_text is None else crlf_text
    result = scan_crlf_injection(crlf_target)
    if result is not None:
        return result

    if _detector_enabled(route.outbound_detectors, "token_patterns"):
        result = scan_token_patterns(text, location="body", safe_tokens=safe_tokens)
        if result is not None:
            return result

    if _detector_enabled(route.outbound_detectors, "known_secrets"):
        # BOT_BOTTLE_SENSITIVE_PREFIXES lets operators add extra env prefixes
        # beyond EGRESS_TOKEN_* without changing the manifest schema.
        extra_raw = environ.get("BOT_BOTTLE_SENSITIVE_PREFIXES", "")
        extra = tuple(p for p in extra_raw.split(",") if p)
        sensitive_prefixes = ("EGRESS_TOKEN_",) + extra
        result = scan_known_secrets(
            text, location="body", env=environ,
            sensitive_prefixes=sensitive_prefixes, safe_tokens=safe_tokens,
        )
        if result is not None:
            return result

    # Entropy scanning requires explicit opt-in: it is NOT part of the
    # default "all detectors" set because it produces false positives on
    # legitimate base64 / binary payloads.  Routes must list "entropy" in
    # dlp.outbound_detectors to enable it.
    if (
        route.outbound_detectors is not None
        and "entropy" in route.outbound_detectors
    ):
        result = scan_entropy(text, location="body")
        if result is not None:
            return result

    return None


def build_token_allow_payload(
    host: str,
    method: str,
    path: str,
    result: ScanResult,
) -> str:
    """Render the human-readable supervisor proposal body for an outbound
    token block (PRD 0062). Carries the host/method/path, the detector
    reason, and the redacted context snippet — never the raw token value."""
    lines = [
        "egress blocked an outbound request carrying a detected token",
        f"host: {host}",
        f"method: {method}",
        f"path: {path}",
        f"detector: {result.reason}",
    ]
    if result.context:
        lines.append(f"context: {result.context}")
    return "\n".join(lines) + "\n"


def scan_inbound(
    route: Route,
    body: str | bytes,
) -> ScanResult | None:
    if route.dlp_passthrough:
        return None
    try:
        from dlp_detectors import scan_naive_injection  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - host-side path
        from .dlp_detectors import scan_naive_injection  # type: ignore[import-not-found]

    text = body if isinstance(body, str) else body.decode("utf-8", errors="replace")

    if _detector_enabled(route.inbound_detectors, "naive_injection_detection"):
        result = scan_naive_injection(text)
        if result is not None:
            return result

    return None


__all__ = [
    "LOG_BLOCKS",
    "route_to_yaml_dict",
    "LOG_FULL",
    "LOG_OFF",
    "ON_MATCH_BLOCK",
    "ON_MATCH_REDACT",
    "ON_MATCH_SUPERVISE",
    "OUTBOUND_ON_MATCH_VALUES",
    "DEFAULT_OUTBOUND_ON_MATCH",
    "OUTBOUND_DETECTOR_NAMES",
    "INBOUND_DETECTOR_NAMES",
    "parse_dlp_block",
    "Config",
    "Decision",
    "HeaderMatch",
    "MatchEntry",
    "PathMatch",
    "Route",
    "ScanResult",
    "build_inbound_scan_text",
    "build_outbound_scan_text",
    "build_token_allow_payload",
    "decide",
    "decide_git_fetch",
    "evaluate_matches",
    "is_git_push_request",
    "is_git_fetch_request",
    "load_config",
    "DENY_UNATTRIBUTED",
    "DENY_UNPARSEABLE",
    "DENY_RESOLVER_ERROR",
    "resolve_client_config",
    "resolve_client_context",
    "PolicyResolverLike",
    "ContextResolverLike",
    "match_route",
    "outbound_scan_headers",
    "parse_config",
    "parse_routes",
    "scan_inbound",
    "scan_outbound",
]
