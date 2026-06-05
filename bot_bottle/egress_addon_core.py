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
    outbound_detectors: tuple[str, ...] | None = None
    inbound_detectors: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Decision:
    action: str  # "forward" or "block"
    reason: str = ""
    inject_authorization: str | None = None


@dataclass(frozen=True)
class ScanResult:
    severity: str   # "block" or "warn"
    reason: str


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

    # dlp detectors
    outbound_detectors, inbound_detectors = _parse_detectors(
        idx, host, raw_dict,
    )

    for k in raw_dict:
        if k not in ("host", "matches", "auth_scheme", "token_env", "dlp"):
            raise ValueError(
                f"{label} ({host}): unknown key {k!r}; accepted keys "
                f"are 'host', 'matches', 'auth_scheme', 'token_env', 'dlp'"
            )

    return Route(
        host=host,
        matches=matches,
        auth_scheme=auth_scheme,
        token_env=token_env,
        outbound_detectors=outbound_detectors,
        inbound_detectors=inbound_detectors,
    )


def load_routes(text: str) -> tuple[Route, ...]:
    """Parse YAML text → routes."""
    try:
        payload = parse_yaml_subset(text)
    except YamlSubsetError as e:
        raise ValueError(f"routes payload: invalid YAML: {e}") from e
    return parse_routes(payload)


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


# ---------------------------------------------------------------------------
# DLP scan dispatch (PRD 0053)
# ---------------------------------------------------------------------------

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
) -> ScanResult | None:
    # Lazy import to avoid circular deps and keep dlp_detectors optional
    # at import time (the sidecar copies it flat alongside this file).
    try:
        from dlp_detectors import scan_token_patterns, scan_known_secrets  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - host-side path
        from .dlp_detectors import scan_token_patterns, scan_known_secrets  # type: ignore[import-not-found]

    text = body if isinstance(body, str) else body.decode("utf-8", errors="replace")

    if _detector_enabled(route.outbound_detectors, "token_patterns"):
        result = scan_token_patterns(text)
        if result is not None:
            return result

    if _detector_enabled(route.outbound_detectors, "known_secrets"):
        result = scan_known_secrets(text, env=environ)
        if result is not None:
            return result

    return None


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
    "Decision",
    "HeaderMatch",
    "MatchEntry",
    "PathMatch",
    "Route",
    "ScanResult",
    "decide",
    "evaluate_matches",
    "is_git_push_request",
    "load_routes",
    "match_route",
    "parse_routes",
    "scan_inbound",
    "scan_outbound",
]
