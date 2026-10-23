"""Egress policy schema parsing and serialization."""

from __future__ import annotations

import re
import typing

from ...contract.yaml_subset import YamlSubsetError, parse_yaml_subset
from .dlp_config import parse_inspect_block
from .types import (
    HEADER_MATCH_TYPES,
    LOG_BLOCKS,
    LOG_FULL,
    LOG_OFF,
    PATH_MATCH_TYPES,
    VALID_METHODS,
    Config,
    HeaderMatch,
    MatchEntry,
    PathMatch,
    Route,
)

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
    legacy_flat = "inspect" not in raw_dict
    inspect_raw = raw_dict.get("inspect", {})
    if inspect_raw is False:
        inspect = False
        settings: dict[str, object] = {}
    elif isinstance(inspect_raw, dict):
        inspect = True
        settings = (
            {k: v for k, v in raw_dict.items() if k != "host"}
            if legacy_flat
            else typing.cast(dict[str, object], inspect_raw)
        )
        legacy_dlp = settings.pop("dlp", None)
        if isinstance(legacy_dlp, dict):
            settings.update(typing.cast(dict[str, object], legacy_dlp))
        elif legacy_dlp is not None:
            raise ValueError(
                f"{label} ({host}): legacy 'dlp' must be an object"
            )
    else:
        raise ValueError(f"{label} ({host}): 'inspect' must be false or an object")

    # matches
    matches: tuple[MatchEntry, ...] = ()
    matches_raw = settings.get("matches")
    if matches_raw is not None:
        if not isinstance(matches_raw, list):
            raise ValueError(f"{label} ({host}): 'matches' must be a list")
        matches_list = typing.cast(list[object], matches_raw)
        matches = tuple(
            _parse_match_entry(idx, k, m) for k, m in enumerate(matches_list)
        )

    # auth (unchanged wire format)
    auth_scheme: object = settings.get("auth_scheme", "")
    token_env: object = settings.get("token_env", "")
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
    git_raw = settings.get("git")
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
    outbound_detectors, inbound_detectors, outbound_on_match = parse_inspect_block(
        idx, host, settings,
    )

    preserve_auth_raw = settings.get("preserve_auth", False)
    if preserve_auth_raw is not True and preserve_auth_raw is not False:
        raise ValueError(
            f"{label} ({host}): 'preserve_auth' must be a boolean"
        )
    preserve_auth: bool = preserve_auth_raw

    for k in settings:
        if k not in (
            "matches", "auth_scheme", "token_env", "git", "preserve_auth",
            "outbound_detectors", "inbound_detectors", "outbound_on_match",
        ):
            raise ValueError(
                f"{label} ({host}): inspect has unknown key {k!r}"
            )
    for k in raw_dict:
        if not legacy_flat and k not in ("host", "inspect"):
            raise ValueError(
                f"{label} ({host}): unknown key {k!r}; accepted keys "
                f"are 'host' and 'inspect'"
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
        inspect=inspect,
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
    if not r.inspect:
        d["inspect"] = False
        return d
    inspected: dict[str, object] = {}
    if r.auth_scheme:
        inspected["auth_scheme"] = r.auth_scheme
        inspected["token_env"] = r.token_env
    if r.matches:
        inspected["matches"] = [_match_entry_to_dict(m) for m in r.matches]
    if r.git_fetch:
        inspected["git"] = {"fetch": True}
    if r.outbound_detectors is not None:
        inspected["outbound_detectors"] = list(r.outbound_detectors)
    if r.inbound_detectors is not None:
        inspected["inbound_detectors"] = list(r.inbound_detectors)
    if r.outbound_on_match:
        inspected["outbound_on_match"] = r.outbound_on_match
    if r.preserve_auth:
        inspected["preserve_auth"] = True
    if inspected:
        d["inspect"] = inspected
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

    delivery = payload_dict.get("audit_delivery", "best_effort")
    if delivery not in ("best_effort", "required"):
        raise ValueError("routes payload: 'audit_delivery' must be best_effort or required")

    routes = parse_routes(payload)
    return Config(routes=routes, log=log_raw, audit_delivery=str(delivery))


def load_config(text: str) -> "Config":
    """Parse YAML text → Config (routes + log flag)."""
    try:
        payload = parse_yaml_subset(text)
    except YamlSubsetError as e:
        raise ValueError(f"routes payload: invalid YAML: {e}") from e
    return parse_config(payload)
