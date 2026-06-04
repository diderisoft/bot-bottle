"""Pure logic for the egress mitmproxy addon (PRD 0017).

Split out of `egress_addon.py` so the host's unit tests can
exercise the parse + decision functions without depending on the
`mitmproxy` package. The companion module wraps these with the
`mitmproxy.http.HTTPFlow` API and is loaded inside the sidecar
container.

Imports: stdlib + `yaml_subset` (which is itself stdlib-only and
ships flat into the sidecar bundle image alongside this file —
see `Dockerfile.sidecars`).
"""

from __future__ import annotations

import typing
from dataclasses import dataclass

# Absolute import — `yaml_subset.py` is copied flat into the bundle
# image's `/app/` next to this file (via `Dockerfile.sidecars`).
# The host-side unit tests run with the repo on sys.path, where the
# import resolves under the `bot_bottle` package. The try/except
# shim picks whichever import works.
try:
    from yaml_subset import YamlSubsetError, parse_yaml_subset  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - host-side path
    from .yaml_subset import YamlSubsetError, parse_yaml_subset


@dataclass(frozen=True)
class Route:
    """One row of the egress route table.

    `host` is the request's `Host` header (or SNI hostname) to match
    against. `path_allowlist` is an optional tuple of absolute path
    prefixes the request path must start with; empty tuple means no
    path constraint. `auth_scheme` and `token_env` together form the
    credential-injection pair (both set or both empty); a non-empty
    pair tells the addon to overwrite the inbound Authorization with
    `<auth_scheme> <value-of-environ[token_env]>`.
    """

    host: str
    path_allowlist: tuple[str, ...] = ()
    auth_scheme: str = ""
    token_env: str = ""


@dataclass(frozen=True)
class Decision:
    """The result of `decide()`. Either forward (with optional
    `inject_authorization` header) or block (with a `reason` to surface
    to the agent)."""

    action: str  # "forward" or "block"
    reason: str = ""
    inject_authorization: str | None = None


def parse_routes(payload: object) -> tuple[Route, ...]:
    """Parse the routes-file payload (already JSON-decoded) into a
    tuple of `Route`s. Raises `ValueError` on any malformed entry —
    the caller decides whether to keep the old table or refuse to
    start.

    Schema:
      {
        "routes": [
          {
            "host": "api.github.com",
            "path_allowlist": ["/repos/x/", "/users/x"],   # optional
            "auth_scheme": "Bearer",                       # optional
            "token_env": "EGRESS_TOKEN_0"            # optional
          },
          ...
        ]
      }
    """
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

    path_allow_raw: object = raw_dict.get("path_allowlist", [])
    if not isinstance(path_allow_raw, list):
        raise ValueError(f"{label} ({host}): 'path_allowlist' must be a list")
    path_allow_list: list[object] = typing.cast(list[object], path_allow_raw)
    prefixes: list[str] = []
    for j, p in enumerate(path_allow_list):
        if not isinstance(p, str):
            raise ValueError(
                f"{label} ({host}): path_allowlist[{j}] must be a string"
            )
        if not p.startswith("/"):
            raise ValueError(
                f"{label} ({host}): path_allowlist[{j}] {p!r} must be an "
                f"absolute path prefix starting with '/'"
            )
        prefixes.append(p)

    auth_scheme: object = raw_dict.get("auth_scheme", "")
    token_env: object = raw_dict.get("token_env", "")
    if not isinstance(auth_scheme, str):
        raise ValueError(f"{label} ({host}): 'auth_scheme' must be a string")
    if not isinstance(token_env, str):
        raise ValueError(f"{label} ({host}): 'token_env' must be a string")
    # Both-or-neither: 'auth' on the manifest side renders to this
    # pair atomically. A partial pair here means the renderer or a
    # hand-edited file is broken.
    if bool(auth_scheme) != bool(token_env):
        raise ValueError(
            f"{label} ({host}): 'auth_scheme' and 'token_env' must be both "
            f"set or both empty (got auth_scheme={auth_scheme!r}, "
            f"token_env={token_env!r})"
        )

    return Route(
        host=host,
        path_allowlist=tuple(prefixes),
        auth_scheme=auth_scheme,
        token_env=token_env,
    )


def load_routes(text: str) -> tuple[Route, ...]:
    """Parse YAML text → routes. Raises `ValueError` for both
    decode and shape errors so callers handle them uniformly.
    `YamlSubsetError` from the parser is a `ValueError` subclass so
    it already satisfies the same surface; we let it propagate."""
    try:
        payload = parse_yaml_subset(text)
    except YamlSubsetError as e:
        raise ValueError(f"routes payload: invalid YAML: {e}") from e
    return parse_routes(payload)


def is_git_push_request(path: str, query: str) -> bool:
    """Return True if the request is a git smart-HTTP push.

    git push over HTTPS hits two endpoints:
      GET <repo>/info/refs?service=git-receive-pack   (capabilities)
      POST <repo>/git-receive-pack                    (the push)

    Fetches use `service=git-upload-pack` / `/git-upload-pack` and
    are unaffected. Egress-proxy refuses HTTPS push because git-gate's
    pre-receive gitleaks scan is the gate for outbound git data;
    routing push through egress would bypass that. Use the
    bottle.git SSH path if you need to push.

    Universal across routes — the block fires even when no
    egress route matches the host. A bare-pass route (host with
    no auth, no path_allowlist) would otherwise let push through to
    the upstream untouched.
    """
    if path.endswith("/git-receive-pack"):
        return True
    if path.endswith("/info/refs"):
        # Query string is parsed leniently — `service=git-receive-pack`
        # may appear with other params in any order.
        for pair in query.split("&"):
            k, _, v = pair.partition("=")
            if k == "service" and v == "git-receive-pack":
                return True
    return False


def match_route(
    routes: typing.Sequence[Route],
    request_host: str,
) -> Route | None:
    """Return the first route whose `host` matches `request_host`
    exactly (case-insensitive). DNS names are case-insensitive.

    Wildcard hosts (`*.foo.com`) are NOT supported — they caused
    too many edge cases (apex match? cert validation?) for too
    little payoff. Operators that need
    multiple subdomains declare them individually (or one common
    parent host as a bare-pass route)."""
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
) -> Decision:
    """Pure decision: given a route table + request host + path + env,
    return what the addon should do with the request.

    - No matching route → BLOCK. The route table is the bottle's
      egress allowlist. A bottle that wants a
      host reachable from the agent must declare a route for it
      (bare-pass route — no `auth`, no `path_allowlist` — is fine
      for hosts that just need passthrough).
    - Matching route with `path_allowlist` set, request path doesn't
      start with any of the allowed prefixes → block with a clear
      reason.
    - Matching route with an auth pair → forward + inject
      Authorization. Token comes from `environ[route.token_env]`;
      missing/empty values block (route declared auth but the secret
      isn't here — operator misconfig).
    """
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

    if route.path_allowlist:
        if not any(request_path.startswith(p) for p in route.path_allowlist):
            return Decision(
                action="block",
                reason=(
                    f"egress: path {request_path!r} not in "
                    f"path_allowlist for {route.host!r}"
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


__all__ = [
    "Decision",
    "Route",
    "decide",
    "is_git_push_request",
    "load_routes",
    "match_route",
    "parse_routes",
]
