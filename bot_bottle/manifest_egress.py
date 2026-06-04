"""Egress routing manifest dataclasses and helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from .manifest_util import ManifestError, as_json_object


# Auth schemes for the egress route's optional `auth` block.
# Same values cred-proxy accepts today; `token` sidesteps the Gitea
# token-not-Bearer quirk (go-gitea/gitea#16734).
EGRESS_AUTH_SCHEMES = ("Bearer", "token")


def validate_egress_routes(
    bottle_name: str,
    routes: tuple[EgressRoute, ...],
) -> None:
    """Cross-validation for `bottle.egress.routes`: hosts must be unique.

    The proxy matches by exact-host (v1); duplicate hosts leave the
    route choice ambiguous so we reject them up front.

    No cross-validation against `bottle.git-gate.repos` is performed.
    git-gate (SSH push/fetch) and egress (HTTPS) broker different
    protocols; declaring both for the same host is a legitimate dev
    setup."""
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
class EgressRoute:
    """One route on the per-bottle egress sidecar (PRD 0017).

    `Host` matches the request's hostname (case-insensitive). The
    optional `PathAllowlist` constrains the URL path to a set of
    prefixes; empty tuple means no path-level filtering. The optional
    `AuthScheme` / `TokenRef` pair drives credential injection:
    when set, the proxy strips any inbound Authorization and injects
    `<AuthScheme> <value-of-host-env-named-by-TokenRef>`. When the
    manifest's `auth` block is omitted both fields are empty strings —
    no Authorization is written, no token forwarded.

    `Role` is reserved for future use; all role strings are currently
    rejected by the validator.

    Validation rules (enforced in `from_dict`):
      - `host` required, non-empty.
      - `path_allowlist` optional, list of absolute path prefixes.
      - `auth` optional. If present, MUST carry both `scheme` and
        `token_ref` as non-empty strings; an empty `auth: {}` is an
        error rather than a synonym for "no auth" (omit `auth` for
        that case).
      - `role` optional, reserved — any non-empty value is rejected.
    """

    Host: str
    PathAllowlist: tuple[str, ...] = ()
    AuthScheme: str = ""
    TokenRef: str = ""
    Role: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, bottle_name: str, idx: int, raw: object) -> "EgressRoute":
        label = f"bottle '{bottle_name}' egress.routes[{idx}]"
        d = as_json_object(raw, label)
        host = d.get("host")
        if not isinstance(host, str) or not host:
            raise ManifestError(f"{label} missing required string field 'host'")

        path_allow_raw = d.get("path_allowlist")
        prefixes: tuple[str, ...] = ()
        if path_allow_raw is not None:
            if not isinstance(path_allow_raw, list):
                raise ManifestError(
                    f"{label} path_allowlist must be an array "
                    f"(was {type(path_allow_raw).__name__})"
                )
            path_list = cast(list[object], path_allow_raw)
            collected: list[str] = []
            for j, p in enumerate(path_list):
                if not isinstance(p, str):
                    raise ManifestError(
                        f"{label} path_allowlist[{j}] must be a string "
                        f"(was {type(p).__name__})"
                    )
                if not p.startswith("/"):
                    raise ManifestError(
                        f"{label} path_allowlist[{j}] {p!r} must be an "
                        f"absolute path prefix starting with '/'"
                    )
                collected.append(p)
            prefixes = tuple(collected)

        auth_scheme = ""
        token_ref = ""
        if "auth" in d:
            auth_raw = d.get("auth")
            auth_d = as_json_object(auth_raw, f"{label} auth")
            if not auth_d:
                raise ManifestError(
                    f"{label} auth is empty ({{}}); omit the 'auth' key "
                    f"entirely if this route is unauthenticated. Otherwise "
                    f"both 'scheme' and 'token_ref' are required."
                )
            auth_scheme_raw = auth_d.get("scheme")
            if not isinstance(auth_scheme_raw, str) or not auth_scheme_raw:
                raise ManifestError(
                    f"{label} auth.scheme is required when 'auth' is set "
                    f"(non-empty string)"
                )
            if auth_scheme_raw not in EGRESS_AUTH_SCHEMES:
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
                if k not in ("scheme", "token_ref"):
                    raise ManifestError(
                        f"{label} auth has unknown key {k!r}; "
                        f"only 'scheme' and 'token_ref' are accepted"
                    )
            auth_scheme = auth_scheme_raw
            token_ref = token_ref_raw

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

        for k in d:
            if k not in ("host", "path_allowlist", "auth", "role"):
                raise ManifestError(
                    f"{label} has unknown key {k!r}; accepted keys are "
                    f"'host', 'path_allowlist', 'auth', 'role'"
                )

        return cls(
            Host=host,
            PathAllowlist=prefixes,
            AuthScheme=auth_scheme,
            TokenRef=token_ref,
            Role=roles,
        )


@dataclass(frozen=True)
class EgressConfig:
    """Per-bottle egress configuration. Today this is just the
    route table; the nesting under `egress:` leaves room for
    per-bottle proxy settings (port override, log level, etc.) in
    follow-ups."""

    routes: tuple[EgressRoute, ...] = ()

    @classmethod
    def from_dict(cls, bottle_name: str, raw: object) -> "EgressConfig":
        d = as_json_object(raw, f"bottle '{bottle_name}' egress")
        routes_raw = d.get("routes")
        routes: tuple[EgressRoute, ...] = ()
        if routes_raw is not None:
            if not isinstance(routes_raw, list):
                raise ManifestError(
                    f"bottle '{bottle_name}' egress.routes must be an array "
                    f"(was {type(routes_raw).__name__})"
                )
            routes_list = cast(list[object], routes_raw)
            routes = tuple(
                EgressRoute.from_dict(bottle_name, i, entry)
                for i, entry in enumerate(routes_list)
            )
            validate_egress_routes(bottle_name, routes)
        for k in d:
            if k != "routes":
                raise ManifestError(
                    f"bottle '{bottle_name}' egress has unknown key {k!r}; "
                    f"only 'routes' is accepted"
                )
        return cls(routes=routes)
