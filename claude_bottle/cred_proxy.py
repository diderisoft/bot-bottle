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
from .manifest import Bottle


@dataclass(frozen=True)
class CredProxyUpstream:
    """One route on the cred-proxy sidecar. Maps a path under the
    proxy to a real upstream, an auth scheme, an in-container env-var
    slot, and optional provisioner roles.

    `path` is the agent-facing prefix (e.g. `/anthropic/`).
    `upstream` is the upstream base URL with scheme. `auth_scheme`
    is the literal word that precedes the token in the injected
    header (`Bearer` for most upstreams; `token` for Gitea —
    sidesteps go-gitea/gitea#16734).

    `token_env` is the env-var name inside the cred-proxy container
    (e.g. `CRED_PROXY_TOKEN_0`); `token_ref` is the host env var the
    CLI reads at launch and forwards into the container's environ
    under `token_env`. Routes that share a TokenRef coalesce to one
    `token_env` slot.

    `roles` are the provisioner tags from the manifest route (see
    `manifest.CRED_PROXY_ROLES`). Each tag drives one agent-side
    rewrite when this upstream's dotfile family is written."""

    path: str
    upstream: str
    auth_scheme: str
    token_env: str
    token_ref: str
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class CredProxyPlan:
    """Output of CredProxy.prepare; consumed by .start.

    The slug + routes_path + upstreams + token_env_map fields are
    filled at prepare time (host-side, side-effect-free on docker).
    The network + pipelock fields are populated by the backend's
    launch step via `dataclasses.replace` once those resources
    exist. Empty defaults are sentinels meaning "not yet set";
    `.start` validates that they are populated.

    `token_env_map` is `{<token_env in container>: <TokenRef on host>}`.
    The backend's start step reads `os.environ[TokenRef]` and forwards
    the value into the cred-proxy container's environ under
    `token_env`. The plan itself never holds token values — secrets
    never land in a dataclass that might be logged.

    `pipelock_ca_host_path` is the host path of the per-bottle CA
    pipelock will present on bumped TLS handshakes; the cred-proxy
    image's entrypoint runs `update-ca-certificates` over it so the
    proxy's HTTPS client trusts pipelock's CA. `pipelock_proxy_url`
    is the URL cred-proxy sets as `HTTPS_PROXY` in its environ so
    outbound HTTPS traverses pipelock — making pipelock's body
    scanner part of the cred-proxy egress path."""

    slug: str
    routes_path: Path
    upstreams: tuple[CredProxyUpstream, ...]
    token_env_map: dict[str, str]
    internal_network: str = ""
    egress_network: str = ""
    pipelock_ca_host_path: Path = Path()
    pipelock_proxy_url: str = ""


def cred_proxy_upstreams_for_bottle(
    bottle: Bottle,
) -> tuple[CredProxyUpstream, ...]:
    """Lift each `bottle.cred_proxy.routes[]` entry into a
    CredProxyUpstream. Order is preserved so route lookup is stable.

    Token-env slots are assigned per distinct TokenRef: the first
    route with TokenRef "GH_PAT" gets `CRED_PROXY_TOKEN_0`; a second
    route with the same TokenRef shares slot 0. The launch step
    forwards each TokenRef's value from the host environ into the
    sidecar's environ under the matching slot name once.

    Manifest validation already enforced uniqueness rules (no
    duplicate paths, singleton-role enforcement)."""
    out: list[CredProxyUpstream] = []
    slot_for_token: dict[str, str] = {}
    for r in bottle.cred_proxy.routes:
        token_env = slot_for_token.get(r.TokenRef)
        if token_env is None:
            token_env = f"CRED_PROXY_TOKEN_{len(slot_for_token)}"
            slot_for_token[r.TokenRef] = token_env
        out.append(CredProxyUpstream(
            path=r.Path,
            upstream=r.Upstream.rstrip("/"),
            auth_scheme=r.AuthScheme,
            token_env=token_env,
            token_ref=r.TokenRef,
            roles=r.Role,
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
                f"before launching, or remove the corresponding route from "
                f"bottle.cred_proxy.routes."
            )
        if not value:
            die(
                f"cred-proxy: host env var '{token_ref}' is empty. The "
                f"cred-proxy will not inject an empty token; set it to the "
                f"real value or remove the route."
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
    "cred_proxy_render_routes",
    "cred_proxy_resolve_token_values",
    "cred_proxy_token_env_map",
    "cred_proxy_upstreams_for_bottle",
]
