"""Per-bottle credential proxy (PRD 0010).

A fourth per-bottle sidecar that holds API tokens (Anthropic OAuth,
GitHub PAT, Gitea PAT, npm token) and injects them as `Authorization`
headers on the agent's behalf. The agent's environ carries only URLs
pointing at `cred-proxy:<PORT>/<route>`; the upstream credentials live
exclusively in the cred-proxy container's environ.

The boundary is the container line — different PID, mount, and network
namespaces separate the agent's container from the cred-proxy's, so
the agent cannot ptrace into the proxy, cannot read its environ via
/proc, and cannot share memory. Reaching the proxy's environ requires
escaping the agent container, the same threshold pipelock and
git-gate already rely on.

This module defines the abstract proxy (`CredProxy`), its plan
dataclass (`CredProxyPlan`), and the per-route shape
(`CredProxyUpstream`). The sidecar's start/stop lifecycle is backend-
specific and lives on concrete subclasses (see
`claude_bottle/backend/docker/cred_proxy.py`).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from .log import die
from .manifest import Bottle, TokenEntry


@dataclass(frozen=True)
class CredProxyUpstream:
    """One route on the cred-proxy sidecar. Maps a path under the
    proxy to a real upstream, an auth scheme, and the env-var slot
    that holds the token inside the proxy container.

    `kind` is the originating `TokenEntry.Kind`; `path` is the agent-
    facing prefix (e.g. `/anthropic/`); `upstream` is the upstream
    base URL with scheme; `auth_scheme` is the literal word that
    precedes the token in the injected header (`Bearer` for all kinds
    except `gitea`, which uses `token` to sidestep go-gitea/gitea#16734).

    `token_env` is the env-var name inside the cred-proxy container
    (e.g. `CRED_PROXY_TOKEN_0`); `token_ref` is the host env var the
    CLI reads at launch and forwards into the container's environ
    under `token_env`. Two routes that share a TokenRef (the github
    Kind expands into two routes — gh-api and gh-git) carry the same
    `token_env`."""

    kind: str
    path: str
    upstream: str
    auth_scheme: str
    token_env: str
    token_ref: str


@dataclass(frozen=True)
class CredProxyPlan:
    """Output of CredProxy.prepare; consumed by .start.

    The slug + routes_path + upstreams + token_env_map fields are
    filled at prepare time (host-side, side-effect-free on docker).
    The network fields are populated by the backend's launch step
    via `dataclasses.replace` once those networks exist. Empty
    defaults are sentinels meaning "not yet set"; `.start` validates
    that they are populated.

    `token_env_map` is `{<token_env in container>: <TokenRef on host>}`.
    The backend's start step reads `os.environ[TokenRef]` and forwards
    the value into the cred-proxy container's environ under
    `token_env`. The plan itself never holds token values — secrets
    never land in a dataclass that might be logged."""

    slug: str
    routes_path: Path
    upstreams: tuple[CredProxyUpstream, ...]
    token_env_map: dict[str, str]
    internal_network: str = ""
    egress_network: str = ""


# Hardcoded upstream URLs for the non-gitea Kinds. Gitea's URL is per-
# entry (`TokenEntry.Url`).
_KIND_ROUTES: dict[str, tuple[tuple[str, str], ...]] = {
    # kind -> ((path, upstream), ...) — a Kind can produce multiple
    # routes; today only `github` does (api + git endpoints).
    "anthropic": (("/anthropic/", "https://api.anthropic.com"),),
    "github": (
        ("/gh-api/", "https://api.github.com"),
        ("/gh-git/", "https://github.com"),
    ),
    "npm": (("/npm/", "https://registry.npmjs.org"),),
}

# Per-Kind auth header value prefix. Gitea uses `token` (not Bearer);
# everyone else uses Bearer.
_KIND_AUTH_SCHEME: dict[str, str] = {
    "anthropic": "Bearer",
    "github": "Bearer",
    "gitea": "token",
    "npm": "Bearer",
}


def cred_proxy_route_path_for_gitea(host: str) -> str:
    """Agent-facing path for a single Gitea instance. The host segment
    disambiguates routes when multiple gitea entries are declared."""
    return f"/gitea/{host}/"


def cred_proxy_upstreams_for_bottle(
    bottle: Bottle,
) -> tuple[CredProxyUpstream, ...]:
    """Lift every `bottle.tokens[]` entry into one or more
    CredProxyUpstreams. Order is preserved so route lookup is stable.
    Manifest validation already enforced uniqueness rules."""
    out: list[CredProxyUpstream] = []
    for i, t in enumerate(bottle.tokens):
        token_env = f"CRED_PROXY_TOKEN_{i}"
        scheme = _KIND_AUTH_SCHEME[t.Kind]
        if t.Kind == "gitea":
            out.append(CredProxyUpstream(
                kind="gitea",
                path=cred_proxy_route_path_for_gitea(t.UpstreamHost),
                upstream=t.Url.rstrip("/"),
                auth_scheme=scheme,
                token_env=token_env,
                token_ref=t.TokenRef,
            ))
        else:
            for path, upstream in _KIND_ROUTES[t.Kind]:
                out.append(CredProxyUpstream(
                    kind=t.Kind,
                    path=path,
                    upstream=upstream,
                    auth_scheme=scheme,
                    token_env=token_env,
                    token_ref=t.TokenRef,
                ))
    return tuple(out)


def cred_proxy_token_env_map(
    upstreams: tuple[CredProxyUpstream, ...],
) -> dict[str, str]:
    """Collapse the upstream list into `{token_env: TokenRef}`. Two
    routes that share a token (gh-api + gh-git) coalesce; the result
    is the set of env vars the backend's start step must forward into
    the sidecar's environ."""
    out: dict[str, str] = {}
    for u in upstreams:
        existing = out.get(u.token_env)
        if existing is not None and existing != u.token_ref:
            die(
                f"cred-proxy plan conflict: {u.token_env} maps to both "
                f"{existing!r} and {u.token_ref!r}. Two routes sharing a "
                f"token slot must reference the same host env var."
            )
        out[u.token_env] = u.token_ref
    return out


def cred_proxy_render_routes(
    upstreams: tuple[CredProxyUpstream, ...],
) -> str:
    """Serialize the route table for the cred-proxy server to read.
    JSON, no token values, no host env-var names — the only thing
    the proxy needs at runtime is the path → upstream + auth-scheme +
    in-container env-var mapping. The actual token values arrive via
    the container's environ."""
    payload = {
        "routes": [
            {
                "path": u.path,
                "upstream": u.upstream,
                "auth_scheme": u.auth_scheme,
                "token_env": u.token_env,
            }
            for u in upstreams
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def cred_proxy_resolve_token_values(
    token_env_map: dict[str, str],
    host_env: dict[str, str],
) -> dict[str, str]:
    """Read `host_env[TokenRef]` for each entry in `token_env_map` and
    return `{token_env: <value>}`. Dies (with a clear pointer at the
    missing var name) if any TokenRef is unset.

    Pure function: takes the host env as an argument so tests can pass
    a sealed mapping without touching `os.environ`."""
    out: dict[str, str] = {}
    for token_env, token_ref in token_env_map.items():
        value = host_env.get(token_ref)
        if value is None:
            die(
                f"cred-proxy: host env var '{token_ref}' is unset. Set it "
                f"before launching, or remove the corresponding token entry "
                f"from bottle.tokens."
            )
        if not value:
            die(
                f"cred-proxy: host env var '{token_ref}' is empty. The "
                f"cred-proxy will not inject an empty token; set it to the "
                f"real value or remove the token entry."
            )
        out[token_env] = value
    return out


class CredProxy(ABC):
    """The per-bottle credential proxy. Encapsulates the host-side
    prepare (upstream lift + routes.json render + token-env-map
    derivation); the sidecar's start/stop lifecycle is backend-
    specific and lives on concrete subclasses."""

    def prepare(self, bottle: Bottle, slug: str, stage_dir: Path) -> CredProxyPlan:
        """Lift `bottle.tokens` into the upstream table, render the
        routes.json (mode 600) under `stage_dir`, and return the plan.
        Pure host-side, no docker subprocess. The token-env map records
        the mapping the launch step uses to forward values from the
        host's environ into the sidecar's environ.

        Returned plan is incomplete: the launch step must fill
        `internal_network` / `egress_network` via `dataclasses.replace`
        before passing it to `.start`."""
        upstreams = cred_proxy_upstreams_for_bottle(bottle)
        routes_path = stage_dir / "cred_proxy_routes.json"
        routes_path.write_text(cred_proxy_render_routes(upstreams))
        routes_path.chmod(0o600)
        return CredProxyPlan(
            slug=slug,
            routes_path=routes_path,
            upstreams=upstreams,
            token_env_map=cred_proxy_token_env_map(upstreams),
        )

    @abstractmethod
    def start(self, plan: CredProxyPlan) -> str:
        """Bring up the cred-proxy sidecar according to `plan`. Returns
        the target string identifying the running instance — the same
        value to pass to `.stop`. Backend-specific."""

    @abstractmethod
    def stop(self, target: str) -> None:
        """Tear down the cred-proxy sidecar identified by `target` (the
        value `.start` returned). Idempotent: a missing target is
        success. Backend-specific."""


__all__ = [
    "CredProxy",
    "CredProxyPlan",
    "CredProxyUpstream",
    "TokenEntry",
    "cred_proxy_render_routes",
    "cred_proxy_resolve_token_values",
    "cred_proxy_route_path_for_gitea",
    "cred_proxy_token_env_map",
    "cred_proxy_upstreams_for_bottle",
]
