"""Egress route manifest dataclasses."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from .util import ManifestError, as_json_object

EGRESS_AUTH_SCHEMES = ("Bearer", "token")
# `auth.header`: an HTTP header name, not one of these (framing, routing and
# the gateway's cookie handling). Mirrors `gateway/egress/types.py`.
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
RESERVED_AUTH_HEADERS = frozenset({
    "host", "cookie", "proxy-authorization", "content-length",
    "transfer-encoding", "connection", "te", "trailer", "upgrade",
    "keep-alive",
})

PATH_MATCH_TYPES = ("exact", "prefix", "regex")
HEADER_MATCH_TYPES = ("exact", "regex")

VALID_METHODS = frozenset({
    "GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE",
    "CONNECT",
})

OUTBOUND_DETECTOR_NAMES = frozenset({"token_patterns", "known_secrets"})
INBOUND_DETECTOR_NAMES = frozenset({"naive_injection_detection"})

# What the proxy does on an outbound token match.
OUTBOUND_ON_MATCH_VALUES = ("block", "redact", "supervise")


def validate_egress_routes(
    bottle_name: str,
    routes: tuple[ManifestEgressRoute, ...],
) -> None:
    seen_hosts: dict[str, None] = {}
    for r in routes:
        key = r.Host.lower()
        if key in seen_hosts:
            raise ManifestError(
                f"bottle '{bottle_name}' egress.routes has duplicate host "
                f"{r.Host!r}; each host must be unique on the proxy."
            )
        seen_hosts[key] = None


@dataclass(frozen=True)
class ManifestPathMatch:
    Type: str = "prefix"
    Value: str = ""


@dataclass(frozen=True)
class ManifestHeaderMatch:
    Name: str = ""
    Value: str = ""
    Type: str = "exact"


@dataclass(frozen=True)
class ManifestMatchEntry:
    Paths: tuple[ManifestPathMatch, ...] = ()
    Methods: tuple[str, ...] = ()
    Headers: tuple[ManifestHeaderMatch, ...] = ()


@dataclass(frozen=True)
class ManifestEgressRoute:
    Host: str
    Matches: tuple[ManifestMatchEntry, ...] = ()
    AuthScheme: str = ""
    # The header the token goes in; "" is `Authorization`. With no scheme the
    # raw token is sent.
    AuthHeader: str = ""
    TokenRef: str = ""
    Role: tuple[str, ...] = ()
    GitFetch: bool = False
    OutboundDetectors: tuple[str, ...] | None = None
    InboundDetectors: tuple[str, ...] | None = None
    OutboundOnMatch: str = ""
    PreserveAuth: bool = False
    Inspect: bool = True

    @classmethod
    def from_dict(cls, bottle_name: str, idx: int, raw: object) -> "ManifestEgressRoute":
        label = f"bottle '{bottle_name}' egress.routes[{idx}]"
        d = as_json_object(raw, label)
        host = d.get("host")
        if not isinstance(host, str) or not host:
            raise ManifestError(f"{label} missing required string field 'host'")

        inspect_raw = d.get("inspect", {})
        if inspect_raw is False:
            inspect = False
            inspect_d: dict[str, object] = {}
        else:
            inspect = True
            inspect_d = as_json_object(inspect_raw, f"{label} inspect")

        # --- matches ---
        matches: tuple[ManifestMatchEntry, ...] = ()
        matches_raw = inspect_d.get("matches")
        if matches_raw is not None:
            if not isinstance(matches_raw, list):
                raise ManifestError(
                    f"{label} matches must be an array "
                    f"(was {type(matches_raw).__name__})"
                )
            matches_list = cast(list[object], matches_raw)
            entries: list[ManifestMatchEntry] = []
            for k, entry_raw in enumerate(matches_list):
                entries.append(
                    _parse_match_entry(label, k, entry_raw)
                )
            matches = tuple(entries)

        # --- auth ---
        auth_scheme = ""
        auth_header = ""
        token_ref = ""
        if "auth" in inspect_d:
            auth_raw = inspect_d.get("auth")
            auth_d = as_json_object(auth_raw, f"{label} inspect.auth")
            if not auth_d:
                raise ManifestError(
                    f"{label} auth is empty ({{}}); omit the 'auth' key "
                    f"entirely if this route is unauthenticated. Otherwise "
                    f"'token_ref' and 'scheme' or 'header' are required."
                )
            auth_header_raw = auth_d.get("header", "")
            if not isinstance(auth_header_raw, str):
                raise ManifestError(
                    f"{label} auth.header must be a string "
                    f"(was {type(auth_header_raw).__name__})"
                )
            if auth_header_raw and not _HEADER_NAME.match(auth_header_raw):
                raise ManifestError(
                    f"{label} auth.header {auth_header_raw!r} is not a valid "
                    f"HTTP header name"
                )
            if auth_header_raw.lower() in RESERVED_AUTH_HEADERS:
                raise ManifestError(
                    f"{label} auth.header {auth_header_raw!r} is reserved"
                )
            # With a named header and no scheme, the raw token is sent.
            auth_scheme_raw = auth_d.get("scheme", "" if auth_header_raw else None)
            if not isinstance(auth_scheme_raw, str) or not (
                auth_scheme_raw or auth_header_raw
            ):
                raise ManifestError(
                    f"{label} auth.scheme is required when 'auth' is set "
                    f"without 'header' (non-empty string)"
                )
            if auth_scheme_raw and auth_scheme_raw not in EGRESS_AUTH_SCHEMES:
                raise ManifestError(
                    f"{label} auth.scheme {auth_scheme_raw!r} is not one of "
                    f"{', '.join(EGRESS_AUTH_SCHEMES)}"
                )
            token_ref_raw = auth_d.get("token_ref")
            if not isinstance(token_ref_raw, str) or not token_ref_raw:
                raise ManifestError(
                    f"{label} auth.token_ref is required when 'auth' is set "
                    f"(name of the host env var holding the token value)"
                )
            for k in auth_d:
                if k not in ("scheme", "header", "token_ref"):
                    raise ManifestError(
                        f"{label} auth has unknown key {k!r}; "
                        f"only 'scheme', 'header' and 'token_ref' are accepted"
                    )
            auth_scheme = auth_scheme_raw
            auth_header = auth_header_raw.lower()
            token_ref = token_ref_raw

        # --- role (reserved) ---
        role_raw = d.get("role")
        roles: tuple[str, ...] = ()
        if role_raw is None:
            roles = ()
        elif isinstance(role_raw, str):
            roles = (role_raw,)
        elif isinstance(role_raw, list):
            role_list = cast(list[object], role_raw)
            collected_roles: list[str] = []
            for r in role_list:
                if not isinstance(r, str):
                    msg = f"{label} role items must be strings (got {type(r).__name__})"
                    raise ManifestError(msg)
                collected_roles.append(r)
            roles = tuple(collected_roles)
        else:
            raise ManifestError(
                f"{label} role must be a string or a list of strings "
                f"(was {type(role_raw).__name__})"
            )
        if roles:
            raise ManifestError(
                f"{label} role {roles[0]!r} is not accepted; "
                f"the 'role' field is reserved for future use"
            )

        # --- DLP settings ---
        outbound_detectors: tuple[str, ...] | None = None
        inbound_detectors: tuple[str, ...] | None = None
        outbound_on_match = ""
        if inspect:
            outbound_detectors, inbound_detectors, outbound_on_match = _parse_inspect_block(
                label, inspect_d,
            )

        # --- git-over-HTTPS policy ---
        git_fetch = False
        if "git" in inspect_d:
            git_d = as_json_object(inspect_d.get("git"), f"{label} inspect.git")
            raw_fetch = git_d.get("fetch", False)
            if isinstance(raw_fetch, bool):
                git_fetch = raw_fetch
            else:
                raise ManifestError(
                    f"{label} git.fetch must be a boolean "
                    f"(was {type(raw_fetch).__name__})"
                )
            for k in git_d:
                if k != "fetch":
                    raise ManifestError(
                        f"{label} git has unknown key {k!r}; "
                        f"only 'fetch' is accepted"
                    )

        # --- preserve_auth ---
        preserve_auth = False
        if "preserve_auth" in inspect_d:
            raw_preserve_auth = inspect_d.get("preserve_auth")
            if not isinstance(raw_preserve_auth, bool):
                raise ManifestError(
                    f"{label} preserve_auth must be a boolean "
                    f"(was {type(raw_preserve_auth).__name__})"
                )
            preserve_auth = raw_preserve_auth

        for k in inspect_d:
            if k not in (
                "matches", "auth", "git", "preserve_auth",
                "outbound_detectors", "inbound_detectors", "outbound_on_match",
            ):
                raise ManifestError(
                    f"{label} inspect has unknown key {k!r}; accepted keys are "
                    f"'matches', 'auth', 'git', 'preserve_auth', "
                    f"'outbound_detectors', 'inbound_detectors', "
                    f"'outbound_on_match'"
                )
        for k in d:
            if k not in ("host", "role", "inspect"):
                raise ManifestError(
                    f"{label} has unknown key {k!r}; accepted keys are "
                    f"'host', 'role', and 'inspect'"
                )

        return cls(
            Host=host,
            Matches=matches,
            AuthScheme=auth_scheme,
            AuthHeader=auth_header,
            TokenRef=token_ref,
            Role=roles,
            GitFetch=git_fetch,
            OutboundDetectors=outbound_detectors,
            InboundDetectors=inbound_detectors,
            OutboundOnMatch=outbound_on_match,
            PreserveAuth=preserve_auth,
            Inspect=inspect,
        )


def _parse_match_entry(
    route_label: str, k: int, raw: object,
) -> ManifestMatchEntry:
    label = f"{route_label} matches[{k}]"
    d = as_json_object(raw, label)

    paths: tuple[ManifestPathMatch, ...] = ()
    paths_raw = d.get("paths")
    if paths_raw is not None:
        if not isinstance(paths_raw, list):
            raise ManifestError(f"{label} paths must be an array")
        paths_list = cast(list[object], paths_raw)
        parsed_paths: list[ManifestPathMatch] = []
        for j, p_raw in enumerate(paths_list):
            parsed_paths.append(_parse_path_match(label, j, p_raw))
        paths = tuple(parsed_paths)

    methods: tuple[str, ...] = ()
    methods_raw = d.get("methods")
    if methods_raw is not None:
        if not isinstance(methods_raw, list):
            raise ManifestError(f"{label} methods must be an array")
        methods_list = cast(list[object], methods_raw)
        normalised: list[str] = []
        for j, m in enumerate(methods_list):
            if not isinstance(m, str):
                raise ManifestError(
                    f"{label} methods[{j}] must be a string"
                )
            upper = m.upper()
            if upper not in VALID_METHODS:
                raise ManifestError(
                    f"{label} methods[{j}] {m!r} is not a valid HTTP method"
                )
            normalised.append(upper)
        methods = tuple(normalised)

    headers: tuple[ManifestHeaderMatch, ...] = ()
    headers_raw = d.get("headers")
    if headers_raw is not None:
        if not isinstance(headers_raw, list):
            raise ManifestError(f"{label} headers must be an array")
        headers_list = cast(list[object], headers_raw)
        parsed_headers: list[ManifestHeaderMatch] = []
        for j, h_raw in enumerate(headers_list):
            parsed_headers.append(_parse_header_match(label, j, h_raw))
        headers = tuple(parsed_headers)

    for key in d:
        if key not in ("paths", "methods", "headers"):
            raise ManifestError(f"{label} has unknown key {key!r}")

    return ManifestMatchEntry(Paths=paths, Methods=methods, Headers=headers)


def _parse_path_match(
    entry_label: str, j: int, raw: object,
) -> ManifestPathMatch:
    label = f"{entry_label} paths[{j}]"
    d = as_json_object(raw, label)
    ptype = d.get("type", "prefix")
    if not isinstance(ptype, str) or ptype not in PATH_MATCH_TYPES:
        raise ManifestError(
            f"{label} type must be one of {', '.join(PATH_MATCH_TYPES)} "
            f"(got {ptype!r})"
        )
    value = d.get("value")
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{label} value must be a non-empty string")
    if ptype in ("exact", "prefix") and not value.startswith("/"):
        raise ManifestError(
            f"{label} value {value!r} must start with '/' for type {ptype!r}"
        )
    if ptype == "regex":
        try:
            re.compile(value)
        except re.error as e:
            raise ManifestError(
                f"{label} regex {value!r} failed to compile: {e}"
            ) from e
    for k in d:
        if k not in ("type", "value"):
            raise ManifestError(f"{label} has unknown key {k!r}")
    return ManifestPathMatch(Type=ptype, Value=value)


def _parse_header_match(
    entry_label: str, j: int, raw: object,
) -> ManifestHeaderMatch:
    label = f"{entry_label} headers[{j}]"
    d = as_json_object(raw, label)
    name = d.get("name")
    if not isinstance(name, str) or not name:
        raise ManifestError(f"{label} name must be a non-empty string")
    value = d.get("value")
    if not isinstance(value, str):
        raise ManifestError(f"{label} value must be a string")
    htype = d.get("type", "exact")
    if not isinstance(htype, str) or htype not in HEADER_MATCH_TYPES:
        raise ManifestError(
            f"{label} type must be one of {', '.join(HEADER_MATCH_TYPES)} "
            f"(got {htype!r})"
        )
    if htype == "regex":
        try:
            re.compile(value)
        except re.error as e:
            raise ManifestError(
                f"{label} regex {value!r} failed to compile: {e}"
            ) from e
    for k in d:
        if k not in ("name", "value", "type"):
            raise ManifestError(f"{label} has unknown key {k!r}")
    return ManifestHeaderMatch(Name=name, Value=value, Type=htype)


def _parse_inspect_block(
    route_label: str,
    inspect: dict[str, object],
) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None, str]:
    """Parse DLP settings from an inspected route."""
    label = f"{route_label} inspect"
    d = inspect

    def _parse_field(
        field: str,
        valid_names: frozenset[str],
    ) -> tuple[str, ...] | None:
        val = d.get(field)
        if val is None:
            return None
        if val is False:
            return ()
        if not isinstance(val, list):
            raise ManifestError(
                f"{label} {field} must be false, a list, or omitted"
            )
        items = cast(list[object], val)
        names: list[str] = []
        for j, item in enumerate(items):
            if not isinstance(item, str):
                raise ManifestError(
                    f"{label} {field}[{j}] must be a string"
                )
            if item not in valid_names:
                raise ManifestError(
                    f"{label} {field}[{j}] {item!r} is not a valid "
                    f"detector; valid: {', '.join(sorted(valid_names))}"
                )
            names.append(item)
        return tuple(names)

    outbound = _parse_field("outbound_detectors", OUTBOUND_DETECTOR_NAMES)
    inbound = _parse_field("inbound_detectors", INBOUND_DETECTOR_NAMES)

    on_match = ""
    on_match_raw = d.get("outbound_on_match")
    if on_match_raw is not None:
        if not isinstance(on_match_raw, str) or on_match_raw not in OUTBOUND_ON_MATCH_VALUES:
            raise ManifestError(
                f"{label} outbound_on_match must be one of "
                f"{', '.join(OUTBOUND_ON_MATCH_VALUES)} (got {on_match_raw!r})"
            )
        on_match = on_match_raw

    return outbound, inbound, on_match


LOG_LEVELS = frozenset({0, 1, 2})


def _blocked_host(label: str, d: dict[str, object]) -> str:
    """The host a `{host: x, blocked: true}` entry blocks."""
    if d.get("blocked") is not True:
        raise ManifestError(f"{label} blocked must be true; omit it to route the host")
    extra = sorted(k for k in d if k not in ("host", "blocked"))
    if extra:
        raise ManifestError(f"{label} is blocked, so it takes only 'host' (got {extra[0]!r})")
    host = d.get("host")
    if not isinstance(host, str) or not host:
        raise ManifestError(f"{label} missing required string field 'host'")
    return host


def overlay_egress(earlier: "ManifestEgressConfig", later: "ManifestEgressConfig",
                   log: int) -> "ManifestEgressConfig":
    """`later` over `earlier`, per host (case-insensitive): a later route
    replaces the earlier one in its place, a later block drops it, and new
    hosts follow in `later`'s order. A block stays in the result so a bottle
    stacked after this one still sees it, unless `later` routes that host."""
    replace = {r.Host.lower(): r for r in later.routes}
    blocked = {h.lower() for h in later.Blocked}
    used: set[str] = set()
    routes: list[ManifestEgressRoute] = []
    for r in earlier.routes:
        key = r.Host.lower()
        if key in blocked:
            continue
        if key in replace:
            used.add(key)
            routes.append(replace[key])
        else:
            routes.append(r)
    routes += [r for r in later.routes if r.Host.lower() not in used]
    kept = tuple(h for h in earlier.Blocked
                 if h.lower() not in replace and h.lower() not in blocked)
    return ManifestEgressConfig(routes=tuple(routes), Log=log, Blocked=kept + later.Blocked)


@dataclass(frozen=True)
class ManifestEgressConfig:
    routes: tuple[ManifestEgressRoute, ...] = ()
    Log: int = 0
    # Hosts a `{host, blocked: true}` entry takes away from inherited routes.
    # Never routes: they only remove a parent's route in the merge.
    Blocked: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, bottle_name: str, raw: object) -> "ManifestEgressConfig":
        d = as_json_object(raw, f"bottle '{bottle_name}' egress")
        routes_raw = d.get("routes")
        routes: tuple[ManifestEgressRoute, ...] = ()
        blocked: list[str] = []
        if routes_raw is not None:
            if not isinstance(routes_raw, list):
                raise ManifestError(
                    f"bottle '{bottle_name}' egress.routes must be an array "
                    f"(was {type(routes_raw).__name__})"
                )
            routes_list = cast(list[object], routes_raw)
            parsed: list[ManifestEgressRoute] = []
            for i, entry in enumerate(routes_list):
                if isinstance(entry, dict) and "blocked" in entry:
                    label = f"bottle '{bottle_name}' egress.routes[{i}]"
                    blocked.append(_blocked_host(label, cast(dict[str, object], entry)))
                else:
                    parsed.append(ManifestEgressRoute.from_dict(bottle_name, i, entry))
            routes = tuple(parsed)
            # A host routed and blocked in one bottle is a duplicate too.
            validate_egress_routes(bottle_name, routes + tuple(
                ManifestEgressRoute(Host=h) for h in blocked))
        log_raw = d.get("log", 0)
        if isinstance(log_raw, bool) or not isinstance(log_raw, int) \
                or log_raw not in LOG_LEVELS:
            raise ManifestError(
                f"bottle '{bottle_name}' egress.log must be 0, 1, or 2"
            )
        for k in d:
            if k not in ("routes", "log"):
                raise ManifestError(
                    f"bottle '{bottle_name}' egress has unknown key {k!r}; "
                    f"accepted keys are 'routes', 'log'"
                )
        return cls(routes=routes, Log=log_raw, Blocked=tuple(blocked))
