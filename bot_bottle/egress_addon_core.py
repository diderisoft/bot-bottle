"""Pure logic for the egress mitmproxy addon (PRD 0017, PRD 0053).

Split out of `egress_addon.py` so the host's unit tests can
exercise the parse + decision functions without depending on the
`mitmproxy` package. The companion module wraps these with the
`mitmproxy.http.HTTPFlow` API and is loaded inside the sidecar
container.

Imports: stdlib + `yaml_subset` (which is itself stdlib-only and
ships flat into the sidecar bundle image alongside this file —
see `Dockerfile.sidecars`)."""

from __future__ import annotations

import re
import typing
from dataclasses import dataclass

try:
    from yaml_subset import YamlSubsetError, parse_yaml_subset  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - host-side path
    from .yaml_subset import YamlSubsetError, parse_yaml_subset


# ---------------------------------------------------------------------------
# Match types (Gateway API HTTPRoute vocabulary, PRD 0053)
# ---------------------------------------------------------------------------

PATH_MATCH_TYPES = ("exact", "prefix", "regex")
HEADER_MATCH_TYPES = ("exact", "regex")

VALID_METHODS = frozenset({
    "GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE",
    "CONNECT",
})

OUTBOUND_DETECTOR_NAMES = frozenset({"token_patterns", "known_secrets"})
INBOUND_DETECTOR_NAMES = frozenset({"naive_injection_detection"})


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


LOG_OFF = 0    # no logging
LOG_BLOCKS = 1  # log block/warn events with request context
LOG_FULL = 2    # log block/warn events + full request and response bodies


@dataclass(frozen=True)
class Config:
    routes: tuple[Route, ...]
    log: int = LOG_OFF


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
    # Raw substring the detector matched. Used inside the sidecar to key the
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


def _parse_detectors(
    idx: int,
    host: str,
    raw_dict: dict[str, object],
) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None]:
    """Parse the optional `dlp` block on a route, returning
    (outbound_detectors, inbound_detectors)."""
    dlp_raw = raw_dict.get("dlp")
    if dlp_raw is None:
        return None, None
    label = f"route[{idx}] ({host})"
    if not isinstance(dlp_raw, dict):
        raise ValueError(f"{label}: 'dlp' must be an object")
    dlp = typing.cast(dict[str, object], dlp_raw)

    def _parse_detector_field(
        field: str,
        valid_names: frozenset[str],
    ) -> tuple[str, ...] | None:
        val = dlp.get(field)
        if val is None:
            return None
        if val is False:
            return ()
        if not isinstance(val, list):
            raise ValueError(
                f"{label}: dlp.{field} must be false, a list, or omitted"
            )
        items = typing.cast(list[object], val)
        names: list[str] = []
        for j, item in enumerate(items):
            if not isinstance(item, str):
                raise ValueError(
                    f"{label}: dlp.{field}[{j}] must be a string"
                )
            if item not in valid_names:
                raise ValueError(
                    f"{label}: dlp.{field}[{j}] {item!r} is not a valid "
                    f"detector name; valid names: {', '.join(sorted(valid_names))}"
                )
            names.append(item)
        return tuple(names)

    outbound = _parse_detector_field("outbound_detectors", OUTBOUND_DETECTOR_NAMES)
    inbound = _parse_detector_field("inbound_detectors", INBOUND_DETECTOR_NAMES)

    for k in dlp:
        if k not in ("outbound_detectors", "inbound_detectors"):
            raise ValueError(
                f"{label}: dlp has unknown key {k!r}; accepted keys "
                f"are 'outbound_detectors', 'inbound_detectors'"
            )
    return outbound, inbound


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
    outbound_detectors, inbound_detectors = _parse_detectors(
        idx, host, raw_dict,
    )

    for k in raw_dict:
        if k not in ("host", "matches", "auth_scheme", "token_env", "dlp", "git"):
            raise ValueError(
                f"{label} ({host}): unknown key {k!r}; accepted keys "
                f"are 'host', 'matches', 'auth_scheme', 'token_env', 'dlp', 'git'"
            )

    return Route(
        host=host,
        matches=matches,
        auth_scheme=auth_scheme,
        token_env=token_env,
        git_fetch=git_fetch,
        outbound_detectors=outbound_detectors,
        inbound_detectors=inbound_detectors,
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
    dlp: dict[str, object] = {}
    if r.outbound_detectors is not None:
        dlp["outbound_detectors"] = list(r.outbound_detectors)
    if r.inbound_detectors is not None:
        dlp["inbound_detectors"] = list(r.inbound_detectors)
    if dlp:
        d["dlp"] = dlp
    return d


def load_routes(text: str) -> tuple[Route, ...]:
    """Parse YAML text → routes."""
    try:
        payload = parse_yaml_subset(text)
    except YamlSubsetError as e:
        raise ValueError(f"routes payload: invalid YAML: {e}") from e
    return parse_routes(payload)


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
) -> Decision:
    route = match_route(routes, request_host)
    if route is None:
        return Decision(
            action="block",
            reason=(
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

    Routes that inject sidecar-owned auth always strip the agent's
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
) -> ScanResult | None:
    # Lazy import to avoid circular deps and keep dlp_detectors optional
    # at import time (the sidecar copies it flat alongside this file).
    try:
        from dlp_detectors import (  # type: ignore[import-not-found]
            scan_crlf_injection,
            scan_known_secrets,
            scan_token_patterns,
        )
    except ImportError:  # pragma: no cover - host-side path
        from .dlp_detectors import (  # type: ignore[import-not-found]
            scan_crlf_injection,
            scan_known_secrets,
            scan_token_patterns,
        )

    text = body if isinstance(body, str) else body.decode("utf-8", errors="replace")

    # CRLF injection is never legitimate — runs unconditionally, not gated
    # by outbound_detectors config, and never override-able by safe_tokens.
    result = scan_crlf_injection(text)
    if result is not None:
        return result

    if _detector_enabled(route.outbound_detectors, "token_patterns"):
        result = scan_token_patterns(text, location="body", safe_tokens=safe_tokens)
        if result is not None:
            return result

    if _detector_enabled(route.outbound_detectors, "known_secrets"):
        result = scan_known_secrets(
            text, location="body", env=environ, safe_tokens=safe_tokens,
        )
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
    "load_routes",
    "match_route",
    "outbound_scan_headers",
    "parse_config",
    "parse_routes",
    "scan_inbound",
    "scan_outbound",
]
