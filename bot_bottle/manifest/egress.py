"""Egress route manifest dataclasses."""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, cast

from .. import secret_ref
from .util import ManifestError, as_json_object

EGRESS_AUTH_SCHEMES = ("Bearer", "token")
# `auth.name` (with `type: header`): an HTTP header name, not one of these (framing, routing and
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

OUTBOUND_DETECTOR_NAMES = frozenset({"token_patterns", "known_secrets", "entropy"})
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
    # Each `allow` entry, its methods made explicit: () is any method (`["*"]`),
    # an entry without `methods` gets the route's, else GET and HEAD. A route
    # with `methods` but no `allow` is one entry. None at all is GET/HEAD.
    Matches: tuple[ManifestMatchEntry, ...] = ()
    # From `auth.type` (`_parse_auth`): bearer is scheme "Bearer", token is
    # "token", header is AuthHeader = its `name` with no scheme (the raw value
    # is sent there). "" header is `Authorization`.
    AuthScheme: str = ""
    AuthHeader: str = ""
    # The secret whose value is sent: `auth.value: secret:NAME` gives NAME
    # (from the store); `secret:${VAR}` gives VAR, with TokenFromEnv (from the
    # launching environment).
    TokenRef: str = ""
    TokenFromEnv: bool = False
    Role: tuple[str, ...] = ()
    GitFetch: bool = False
    # From `dlp`: None is the default set, () none, else exactly these.
    OutboundDetectors: tuple[str, ...] | None = None
    InboundDetectors: tuple[str, ...] | None = None
    OutboundOnMatch: str = ""
    PreserveAuth: bool = False
    # False for `passthrough: true`.
    Inspect: bool = True

    @classmethod
    def from_dict(cls, bottle_name: str, idx: int, raw: object) -> "ManifestEgressRoute":
        label = f"bottle '{bottle_name}' egress.routes[{idx}]"
        d = as_json_object(raw, label)
        host = d.get("host")
        if not isinstance(host, str) or not host:
            raise ManifestError(f"{label} missing required string field 'host'")
        old = [k for k in OLD_ROUTE_KEYS if k in d]
        if old:
            raise OldRouteFormatError(bottle_name, host, old[0])
        for k in d:
            if k not in ROUTE_KEYS:
                raise ManifestError(
                    f"{label} has unknown key {k!r}; accepted keys are "
                    f"{', '.join(repr(x) for x in ROUTE_KEYS)} (and 'blocked', alone with "
                    f"'host', to take an inherited route away)")
        passthrough = d.get("passthrough", False)
        if not isinstance(passthrough, bool):
            raise ManifestError(f"{label} passthrough must be true or false")
        if passthrough:
            inspected_only = [k for k in ROUTE_KEYS if k not in ("host", "passthrough")
                              and k in d]
            if inspected_only:
                raise ManifestError(
                    f"{label} is passthrough: the gateway doesn't look inside it, so it "
                    f"takes no {', '.join(inspected_only)}")
            return cls(Host=host, Inspect=False)

        route_methods = (_parse_methods(f"{label} methods", d["methods"])
                         if "methods" in d else None)
        matches: tuple[ManifestMatchEntry, ...] = ()
        if "allow" in d:
            allow_raw = d.get("allow")
            if not isinstance(allow_raw, list) or not allow_raw:
                raise ManifestError(f"{label} allow must be a non-empty list of entries "
                                    f"(paths, methods, headers)")
            matches = tuple(_parse_match_entry(label, k, entry, route_methods)
                            for k, entry in enumerate(cast(list[object], allow_raw)))
        elif route_methods is not None:
            matches = (ManifestMatchEntry(Methods=route_methods),)

        auth_scheme = auth_header = token_ref = ""
        token_from_env = False
        if "auth" in d:
            auth_scheme, auth_header, token_ref, token_from_env = _parse_auth(
                label, bottle_name, host, d.get("auth"))

        git_fetch = False
        if "git" in d:
            git_d = as_json_object(d.get("git"), f"{label} git")
            for k in git_d:
                if k != "fetch":
                    raise ManifestError(f"{label} git has unknown key {k!r}; only 'fetch' "
                                        f"is accepted")
            raw_fetch = git_d.get("fetch", False)
            if not isinstance(raw_fetch, bool):
                raise ManifestError(f"{label} git.fetch must be true or false "
                                    f"(was {type(raw_fetch).__name__})")
            git_fetch = raw_fetch

        preserve_auth = d.get("preserve_auth", False)
        if not isinstance(preserve_auth, bool):
            raise ManifestError(f"{label} preserve_auth must be true or false "
                                f"(was {type(preserve_auth).__name__})")

        outbound, inbound, on_match = _parse_dlp(label, d["dlp"]) if "dlp" in d \
            else (None, None, "")
        return cls(
            Host=host,
            Matches=matches,
            AuthScheme=auth_scheme,
            AuthHeader=auth_header,
            TokenRef=token_ref,
            TokenFromEnv=token_from_env,
            GitFetch=git_fetch,
            OutboundDetectors=outbound,
            InboundDetectors=inbound,
            OutboundOnMatch=on_match,
            PreserveAuth=preserve_auth,
            Inspect=True,
        )


ROUTE_KEYS = ("host", "passthrough", "methods", "allow", "auth", "git", "preserve_auth",
              "dlp")
# Keys a route no longer takes; `bottles migrate` rewrites them.
OLD_ROUTE_KEYS = ("inspect", "role", "matches")
# A method list's "any method".
ANY_METHOD = "*"
DEFAULT_METHODS = ("GET", "HEAD")


def route_entries(route: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """What a route (as written, in the current shape) allows, as `allow`
    entries each with its methods: None for any request (passthrough, or
    `methods: ["*"]` alone); a route with neither `allow` nor `methods` is
    one GET/HEAD entry."""
    if route.get("passthrough") is True:
        return None
    methods = route.get("methods")
    default = list(methods) if isinstance(methods, list) and methods else list(DEFAULT_METHODS)
    allow = route.get("allow")
    if isinstance(allow, list) and allow:
        entries = [{**e, "methods": e.get("methods", default)} for e in allow
                   if isinstance(e, dict)]
    else:
        entries = [{"methods": default}]
    if any(set(e) == {"methods"} and e["methods"] == [ANY_METHOD] for e in entries):
        return None
    return entries


def with_entries(route: Mapping[str, Any], entries: list[dict[str, Any]] | None,
                 ) -> dict[str, Any]:
    """`route` allowing `entries` (`route_entries`' form) instead of what it
    did, said as briefly as the shape allows."""
    out = {k: v for k, v in route.items() if k not in ("methods", "allow")}
    if entries is None:
        out["methods"] = [ANY_METHOD]
    elif len(entries) == 1 and set(entries[0]) == {"methods"}:
        if sorted(entries[0]["methods"]) != sorted(DEFAULT_METHODS):
            out["methods"] = entries[0]["methods"]
    else:
        out["allow"] = [{k: v for k, v in e.items()
                         if not (k == "methods" and sorted(v) == sorted(DEFAULT_METHODS))}
                        for e in entries]
    return out


class OldRouteFormatError(ManifestError):
    """A bottle file still writes a route in the old shape (`inspect`,
    `matches`, `role`); `bottles migrate` rewrites it."""

    def __init__(self, bottle: str, host: str, key: str) -> None:
        self.bottle = bottle
        self.host = host
        what = {
            "inspect": "inspect is gone (auth, git and preserve_auth sit on the route, "
                       "matches is allow, the detectors are dlp, and inspect: false is "
                       "passthrough: true)",
            "matches": "matches is now allow",
            "role": "role is gone",
        }[key]
        super().__init__(f"{bottle} egress route {host}: {what}. "
                         f"Run bot-bottle bottles migrate {bottle}.")


def _parse_methods(label: str, raw: object) -> tuple[str, ...]:
    """A `methods` list: () for `["*"]` (any), else the methods, upper case."""
    if not isinstance(raw, list) or not raw:
        raise ManifestError(f"{label} must be a non-empty list of HTTP methods, or [\"*\"] "
                            f"for any")
    items = cast(list[object], raw)
    if ANY_METHOD in items:
        if len(items) != 1:
            raise ManifestError(f"{label}: \"*\" (any method) stands alone")
        return ()
    out: list[str] = []
    for j, m in enumerate(items):
        if not isinstance(m, str) or m.upper() not in VALID_METHODS:
            raise ManifestError(f"{label}[{j}] {m!r} is not an HTTP method")
        out.append(m.upper())
    return tuple(out)


def _parse_dlp(label: str, raw: object) -> tuple[tuple[str, ...] | None,
                                                   tuple[str, ...] | None, str]:
    """`dlp: false` (no detectors) or `{outbound, inbound, on_match}`: a
    listed side runs exactly those, an omitted one the defaults."""
    if raw is False:
        return (), (), ""
    d = as_json_object(raw, f"{label} dlp")
    for k in d:
        if k not in ("outbound", "inbound", "on_match"):
            raise ManifestError(f"{label} dlp has unknown key {k!r}; accepted keys are "
                                f"'outbound', 'inbound' and 'on_match' (or dlp: false)")

    def names(side: str, valid: frozenset[str]) -> tuple[str, ...] | None:
        if side not in d:
            return None
        value = d[side]
        if not isinstance(value, list):
            raise ManifestError(f"{label} dlp.{side} must be a list of detectors "
                                f"([] for none)")
        for j, item in enumerate(cast(list[object], value)):
            if not isinstance(item, str) or item not in valid:
                raise ManifestError(f"{label} dlp.{side}[{j}] {item!r} is not a detector; "
                                    f"valid: {', '.join(sorted(valid))}")
        return tuple(cast(list[str], value))

    on_match = d.get("on_match", "")
    if on_match and on_match not in OUTBOUND_ON_MATCH_VALUES:
        raise ManifestError(f"{label} dlp.on_match must be one of "
                            f"{', '.join(OUTBOUND_ON_MATCH_VALUES)} (got {on_match!r})")
    return (names("outbound", OUTBOUND_DETECTOR_NAMES), names("inbound", INBOUND_DETECTOR_NAMES),
            str(on_match or ""))


class OldAuthFormatError(ManifestError):
    """A bottle file still writes a route's auth in the old format (`scheme`,
    `header`, `token_ref`), now `type` and `value` (and `name` for a header);
    `bottles migrate` rewrites it (`manifest/migrate.py`)."""

    def __init__(self, bottle: str, host: str) -> None:
        self.bottle = bottle
        self.host = host
        super().__init__(
            f"{bottle} egress route {host}: auth now takes type and value (and name for a "
            f"header). Run bot-bottle bottles migrate {bottle}.")


# `auth.type`, and the scheme each sends in `Authorization` ("" for `header`:
# the raw value in the header `name`).
AUTH_TYPES = {"bearer": "Bearer", "token": "token", "header": ""}
OLD_AUTH_KEYS = ("scheme", "header", "token_ref")


def _parse_auth(label: str, bottle: str, host: str,
                raw: object) -> tuple[str, str, str, bool]:
    """A route's `auth` as (scheme, header, secret name, from env), the fields
    the gateway takes:

      { type: bearer, value: secret:NAME }            Authorization: Bearer <v>
      { type: token,  value: secret:NAME }            Authorization: token <v>
      { type: header, name: X-Api-Key, value: ... }   X-Api-Key: <v> (raw)
    """
    auth = as_json_object(raw, f"{label} auth")
    if not auth:
        raise ManifestError(
            f"{label} auth is empty ({{}}); omit the 'auth' key entirely if this route "
            f"is unauthenticated. Otherwise 'type' and 'value' are required.")
    if any(k in auth for k in OLD_AUTH_KEYS):
        raise OldAuthFormatError(bottle, host)
    for k in auth:
        if k not in ("type", "name", "value"):
            raise ManifestError(f"{label} auth has unknown key {k!r}; only 'type', 'name' "
                                f"(for a header) and 'value' are accepted")
    kind = auth.get("type")
    if not isinstance(kind, str) or kind not in AUTH_TYPES:
        shown = f" (was {kind!r})" if isinstance(kind, str) else ""
        raise ManifestError(f"{label} auth.type is required: one of "
                            f"{', '.join(AUTH_TYPES)}{shown}")
    name = auth.get("name")
    if kind != "header":
        if name is not None:
            raise ManifestError(f"{label} auth.name is only for type: header")
        return (AUTH_TYPES[kind], "", *_auth_token(label, auth))
    if not isinstance(name, str) or not name:
        raise ManifestError(f"{label} auth.name (the header) is required with type: header")
    if not _HEADER_NAME.match(name):
        raise ManifestError(f"{label} auth.name {name!r} is not a valid HTTP header name")
    if name.lower() in RESERVED_AUTH_HEADERS:
        raise ManifestError(f"{label} auth.name {name!r} is reserved")
    return ("", name.lower(), *_auth_token(label, auth))


def _auth_token(label: str, auth: dict[str, object]) -> tuple[str, bool]:
    """An auth block's secret: (NAME, False) from `value: secret:NAME` (the
    store), (VAR, True) from `secret:${VAR}` (the launching env). Not asked
    for (`secret:?`, `?`), not a plain `${VAR}` or a literal."""
    value = auth.get("value")
    if not isinstance(value, str) or not value:
        raise ManifestError(
            f"{label} auth.value is required when 'auth' is set "
            f"(value: secret:NAME, NAME in the secrets file)")
    ref = secret_ref.parse(value)
    if ref.secret and ref.kind == secret_ref.HOST_ENV:
        return ref.name, True
    if ref.kind != secret_ref.STORED:
        why = (" (a route's secret is resolved where no one may be there to type it, so "
               "secret:? isn't taken here)" if ref.secret else "")
        raise ManifestError(f"{label} auth.value takes secret:NAME or secret:${{VAR}} "
                            f"(was {_shown(value)}){why}")
    problem = secret_ref.problem(value)
    if problem:
        raise ManifestError(f"{label} auth.value {problem}")
    return ref.name, False


def _shown(value: str) -> str:
    """How an invalid value is named in an error: a reference as written, a
    literal (perhaps the secret itself) never."""
    if secret_ref.kind(value) in (secret_ref.PROMPT, secret_ref.HOST_ENV):
        return repr(value)  # a reference, never the secret itself
    return "a literal value"


def _parse_match_entry(route_label: str, k: int, raw: object,
                       route_methods: tuple[str, ...] | None) -> ManifestMatchEntry:
    label = f"{route_label} allow[{k}]"
    d = as_json_object(raw, label)
    for key in d:
        if key not in ("paths", "methods", "headers"):
            raise ManifestError(f"{label} has unknown key {key!r}; accepted keys are "
                                f"'paths', 'methods' and 'headers'")
    paths: tuple[ManifestPathMatch, ...] = ()
    if "paths" in d:
        paths_raw = d["paths"]
        if not isinstance(paths_raw, list):
            raise ManifestError(f"{label} paths must be a list")
        paths = tuple(_parse_path_match(label, j, p)
                      for j, p in enumerate(cast(list[object], paths_raw)))
    if "methods" in d:
        methods = _parse_methods(f"{label} methods", d["methods"])
    else:
        methods = route_methods if route_methods is not None else DEFAULT_METHODS
    headers: tuple[ManifestHeaderMatch, ...] = ()
    if "headers" in d:
        headers_raw = d["headers"]
        if not isinstance(headers_raw, list):
            raise ManifestError(f"{label} headers must be a list")
        headers = tuple(_parse_header_match(label, j, h)
                        for j, h in enumerate(cast(list[object], headers_raw)))
    return ManifestMatchEntry(Paths=paths, Methods=methods, Headers=headers)


def _parse_path_match(entry_label: str, j: int, raw: object) -> ManifestPathMatch:
    """A path filter: a bare string is a prefix, `{exact: /x}` exact,
    `{regex: ...}` a regex."""
    label = f"{entry_label} paths[{j}]"
    if isinstance(raw, str):
        ptype, value = "prefix", raw
    else:
        d = as_json_object(raw, label)
        if "type" in d or "value" in d:
            raise ManifestError(
                f"{label}: a path is a prefix string, {{exact: /x}} or {{regex: ...}} "
                f"(no type/value). Run bot-bottle bottles migrate <bottle>.")
        if len(d) != 1 or next(iter(d)) not in ("exact", "regex"):
            raise ManifestError(f"{label} must be a prefix string, {{exact: /x}} or "
                                f"{{regex: ...}}")
        ptype, value = next(iter(d.items()))
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{label} must be a non-empty string")
    if ptype in ("exact", "prefix") and not value.startswith("/"):
        raise ManifestError(f"{label} {value!r} must start with '/'")
    if ptype == "regex":
        try:
            re.compile(value)
        except re.error as e:
            raise ManifestError(f"{label} regex {value!r} failed to compile: {e}") from e
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


# `egress.log`, and the gateway's level for each.
LOG_LEVELS = {"off": 0, "blocked": 1, "full": 2}
LOG_NAMES = {v: k for k, v in LOG_LEVELS.items()}


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
                    f"bottle '{bottle_name}' egress.routes must be a list "
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
        log_raw = d.get("log", "off")
        if isinstance(log_raw, int) and not isinstance(log_raw, bool) and log_raw in LOG_NAMES:
            raise ManifestError(
                f"bottle '{bottle_name}' egress.log: {log_raw} is now "
                f"{LOG_NAMES[log_raw]}. Run bot-bottle bottles migrate {bottle_name}.")
        if not isinstance(log_raw, str) or log_raw not in LOG_LEVELS:
            raise ManifestError(
                f"bottle '{bottle_name}' egress.log must be off, blocked or full")
        for k in d:
            if k not in ("routes", "log"):
                raise ManifestError(
                    f"bottle '{bottle_name}' egress has unknown key {k!r}; "
                    f"accepted keys are 'routes', 'log'"
                )
        return cls(routes=routes, Log=LOG_LEVELS[log_raw], Blocked=tuple(blocked))
